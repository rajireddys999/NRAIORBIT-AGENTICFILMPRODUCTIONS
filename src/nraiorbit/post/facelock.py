"""Speaker lock for lip-sync: LatentSync touches only the speaking character's mouth.

LatentSync redraws the mouth of the largest face it finds in each frame. In a shot with a bigger face in it (an
over-the-shoulder head, a blurred passer-by in the foreground, a henchman nearer the lens) it animates the wrong person
and leaves the speaker's own mouth unsynced in those frames, and whatever else falls inside its face box (a hand, the
water behind a cheek) can be smeared. The speaker lock fixes both without changing the GPU worker:

1. `analyse`: every face in every frame (MediaPipe BlazeFace full-range, Apache-2.0), linked into tracks, with how
   sharp each face is and how much its mouth moves. The video model is told that only the speaker talks, so the
   speaker's mouth is the one that moves.
2. `pick_speaker`: a point from the config (`lipsync.shots.<shot_id>.speaker: [x, y]`, fractions of the frame), else
   the face that talks the most among the sharp, steady ones.
3. `plan_segments` / `write_crop`: LatentSync gets a crop around the speaker with every other face blanked out, so the
   speaker is the only face it can find. Frames where the speaker's face is not visible (turned away, covered) are
   left as filmed.
4. `composite`: the synced pixels are pasted back only inside a feathered mask over the speaker's mouth and jaw.
   Nothing else in the frame changes, the speaker's eyes included (LatentSync softens the whole face it redraws).
5. `edit_locations`: per frame, how much a synced clip changed the speaker's mouth and how much it changed anything
   away from the speaker's face. A frame where LatentSync animated someone else shows a bigger change elsewhere, so
   a whole-frame result can be checked before it is reused.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np

from . import ffmpeg

BLAZEFACE_URL = ("https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/latest/"
                 "blaze_face_full_range.tflite")
ANALYSIS_VERSION = 1
PATCH = (32, 20)                  # mouth / eye patches compared frame to frame (width, height)


# ---------------------------------------------------------------------------------------------------------- faces

@dataclass
class Face:
    box: list[float]              # x0, y0, x1, y1 in pixels
    score: float
    eyes: list[list[float]]       # [[x, y], [x, y]], image-left eye first
    nose: list[float]
    mouth: list[float]
    sharpness: float = 0.0        # focus of the face: variance of the Laplacian of the face resized to 96x96
    talk: float | None = None     # mouth motion since the track's previous frame, minus the eyes' motion
    track: int = -1

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]

    @property
    def centre(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    def scale(self) -> float:
        """Eye distance, kept sensible when the head turns (the eyes close up in profile, the box does not)."""
        (lx, ly), (rx, ry) = self.eyes
        return max(math.hypot(rx - lx, ry - ly), 0.42 * self.width, 1.0)


Detector = Callable[[np.ndarray], list[Face]]


class BlazeFace:
    """MediaPipe's BlazeFace full-range face detector (Apache-2.0). The model file (1 MB) is downloaded once from
    Google's model storage into model_dir. Needs `pip install mediapipe` (the `lipsync` extra)."""

    def __init__(self, model_dir: Path | None = None, min_score: float = 0.35):
        try:
            from mediapipe.tasks.python import BaseOptions, vision
        except ImportError as e:  # pragma: no cover - depends on the machine
            raise RuntimeError("lip-sync speaker lock needs MediaPipe: pip install -e '.[lipsync]' "
                               "(or set lipsync.speaker_lock: false)") from e
        model_dir = Path(model_dir or os.environ.get("NRAIORBIT_MODELS") or Path.home() / ".cache" / "nraiorbit")
        path = model_dir / "blaze_face_full_range.tflite"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            urllib.request.urlretrieve(BLAZEFACE_URL, tmp)
            tmp.replace(path)
        self._det = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(path)), min_detection_confidence=min_score))

    def __call__(self, rgb: np.ndarray) -> list[Face]:
        import mediapipe as mp

        h, w = rgb.shape[:2]
        found = self._det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
        faces = []
        for d in found.detections:
            b = d.bounding_box
            # keypoints: the subject's right eye, left eye, nose tip, mouth centre, right ear, left ear
            kp = [[k.x * w, k.y * h] for k in d.keypoints]
            faces.append(Face([float(b.origin_x), float(b.origin_y), float(b.origin_x + b.width),
                               float(b.origin_y + b.height)], float(d.categories[0].score),
                              sorted(kp[:2]), kp[2], kp[3]))
        return faces


# ------------------------------------------------------------------------------------------------------ video io

def probe(path: Path) -> tuple[int, int, float]:
    """Width, height and frame rate of the first video stream."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,r_frame_rate", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    s = json.loads(out)["streams"][0]
    num, _, den = s["r_frame_rate"].partition("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den or 1)


def read_frames(path: Path) -> Iterator[np.ndarray]:
    """Decoded RGB frames, one at a time."""
    w, h, _ = probe(path)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = w * h * 3
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


class FrameWriter:
    """RGB frames in, an H.264 clip out with the settings every clip of the edit shares (ffmpeg.VIDEO_ENCODE)."""

    def __init__(self, out: Path, width: int, height: int, fps: float):
        out.parent.mkdir(parents=True, exist_ok=True)
        self.out = out
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
             "-r", f"{fps:g}", "-i", "-", "-an", *ffmpeg.VIDEO_ENCODE, str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, rgb: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(rgb, dtype=np.uint8).tobytes())

    def __enter__(self) -> "FrameWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode(errors="replace")
        if self.proc.wait() != 0 and exc[0] is None:
            raise ffmpeg.FFmpegError(f"ffmpeg could not write {self.out}: {err[-1500:]}")


# ---------------------------------------------------------------------------------------------------- analysis

@dataclass
class Track:
    id: int
    faces: dict[int, Face] = field(default_factory=dict)
    presence: float = 0.0         # share of the shot's frames the face is found in
    sharpness: float = 0.0        # median focus
    talk: float = 0.0             # mean mouth motion (net of head motion)
    height: float = 0.0           # median face height in pixels
    score: float = 0.0            # median detection confidence

    def centre(self) -> tuple[float, float]:
        cs = np.array([f.centre for f in self.faces.values()])
        return float(np.median(cs[:, 0])), float(np.median(cs[:, 1]))


@dataclass
class Analysis:
    width: int
    height: int
    fps: float
    frames: int
    tracks: list[Track]

    def to_json(self) -> str:
        return json.dumps({"version": ANALYSIS_VERSION, "width": self.width, "height": self.height, "fps": self.fps,
                           "frames": self.frames,
                           "tracks": [{**{k: v for k, v in asdict(t).items() if k != "faces"},
                                       "faces": {str(i): asdict(f) for i, f in t.faces.items()}}
                                      for t in self.tracks]})

    @classmethod
    def from_json(cls, text: str) -> "Analysis":
        d = json.loads(text)
        if d.get("version") != ANALYSIS_VERSION:
            raise ValueError("old analysis")
        tracks = [Track(**{**{k: v for k, v in t.items() if k != "faces"},
                           "faces": {int(i): Face(**f) for i, f in t["faces"].items()}}) for t in d["tracks"]]
        return cls(d["width"], d["height"], d["fps"], d["frames"], tracks)


def _patch(gray: np.ndarray, cx: float, cy: float, w: float, h: float) -> np.ndarray | None:
    """A face area resized to PATCH and normalised (zero mean, unit spread), so lighting changes do not count."""
    x0, y0, x1, y1 = int(cx - w / 2), int(cy - h / 2), int(cx + w / 2), int(cy + h / 2)
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0] or x1 - x0 < 4 or y1 - y0 < 4:
        return None
    region = gray[y0:y1, x0:x1]
    ys = np.linspace(0, region.shape[0] - 1, PATCH[1]).astype(int)
    xs = np.linspace(0, region.shape[1] - 1, PATCH[0]).astype(int)
    p = region[np.ix_(ys, xs)].astype(np.float32)
    return (p - p.mean()) / (p.std() + 4.0)


def _sharpness(gray: np.ndarray, box: list[float]) -> float:
    x0, y0 = max(0, int(box[0])), max(0, int(box[1]))
    x1, y1 = min(gray.shape[1], int(box[2])), min(gray.shape[0], int(box[3]))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    region = gray[y0:y1, x0:x1]
    ys = np.linspace(0, region.shape[0] - 1, 96).astype(int)
    xs = np.linspace(0, region.shape[1] - 1, 96).astype(int)
    g = region[np.ix_(ys, xs)].astype(np.float32)
    lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
    return float(lap.var())


def _iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def link_tracks(per_frame: list[list[Face]], max_gap: int = 8, min_iou: float = 0.15) -> list[Track]:
    """Greedy frame-to-frame linking: each face joins the open track whose last box overlaps it most (or whose last
    centre is within half a face width), else starts a new track. A track stays open across max_gap missed frames."""
    tracks: list[Track] = []
    last: dict[int, int] = {}
    for i, faces in enumerate(per_frame):
        taken: set[int] = set()
        for f in sorted(faces, key=lambda f: -f.score):
            best, best_v = None, min_iou
            for t in tracks:
                if t.id in taken or i - last[t.id] > max_gap:
                    continue
                prev = t.faces[last[t.id]]
                v = _iou(prev.box, f.box)
                (px, py), (fx, fy) = prev.centre, f.centre
                if v < min_iou and math.hypot(px - fx, py - fy) < 0.5 * max(prev.width, f.width):
                    v = min_iou + 1e-6
                if v > best_v:
                    best, best_v = t, v
            if best is None:
                best = Track(len(tracks))
                tracks.append(best)
            f.track = best.id
            best.faces[i] = f
            last[best.id] = i
            taken.add(best.id)
    return tracks


def _summarise(tracks: list[Track], frames: int) -> None:
    for t in tracks:
        fs = list(t.faces.values())
        t.presence = len(fs) / max(frames, 1)
        t.sharpness = float(np.median([f.sharpness for f in fs]))
        talks = [f.talk for f in fs if f.talk is not None]
        t.talk = float(np.mean(talks)) if talks else 0.0
        t.height = float(np.median([f.height for f in fs]))
        t.score = float(np.median([f.score for f in fs]))


def analyse(video: Path, detector: Detector, cache_dir: Path | None = None) -> Analysis:
    """Faces of every frame of a clip, linked into tracks, with focus and mouth motion. Cached as JSON in cache_dir
    under the clip's content hash, so a re-rendered identical clip is not analysed again."""
    cache = cache_dir / f"{hashlib.sha1(video.read_bytes()).hexdigest()[:16]}.json" if cache_dir else None
    if cache and cache.exists():
        try:
            return Analysis.from_json(cache.read_text(encoding="utf-8"))
        except (ValueError, KeyError, TypeError):
            pass
    w, h, fps = probe(video)
    per_frame: list[list[Face]] = []
    patches: dict[tuple[int, int], tuple[np.ndarray | None, np.ndarray | None]] = {}
    for i, rgb in enumerate(read_frames(video)):
        gray = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
        faces = detector(rgb)
        for j, f in enumerate(faces):
            f.sharpness = _sharpness(gray, f.box)
            s = f.scale()
            (lx, ly), (rx, ry) = f.eyes
            patches[(i, j)] = (_patch(gray, f.mouth[0], f.mouth[1] + 0.1 * s, 1.0 * s, 0.6 * s),
                               _patch(gray, (lx + rx) / 2, (ly + ry) / 2, 1.5 * s, 0.6 * s))
        per_frame.append(faces)
    tracks = link_tracks(per_frame)
    index = {id(f): (i, j) for i, faces in enumerate(per_frame) for j, f in enumerate(faces)}
    for t in tracks:
        prev = None
        for i in sorted(t.faces):
            f = t.faces[i]
            if prev is not None and prev[0] == i - 1:
                m0, e0 = patches[index[id(prev[1])]]
                m1, e1 = patches[index[id(f)]]
                if m0 is not None and m1 is not None and e0 is not None and e1 is not None:
                    f.talk = max(0.0, float(np.abs(m1 - m0).mean() - np.abs(e1 - e0).mean()))
            prev = (i, f)
    _summarise(tracks, len(per_frame))
    result = Analysis(w, h, fps, len(per_frame), tracks)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(result.to_json(), encoding="utf-8")
    return result


def pick_speaker(analysis: Analysis, hint: list[float] | None = None, min_presence: float = 0.25) -> Track | None:
    """The speaking character's face track.

    hint: [x, y] as fractions of the frame, a point on the speaker's face in the frames where they talk: the track
    whose boxes contain it most often (else the nearest). Without a hint: among faces found in at least min_presence
    of the frames and at least half as sharp as the sharpest of them (an out-of-focus foreground head never wins),
    the one whose mouth moves the most; presence breaks near-ties."""
    tracks = [t for t in analysis.tracks if t.faces]
    if not tracks:
        return None
    if hint is not None:
        px, py = hint[0] * analysis.width, hint[1] * analysis.height

        def hits(t: Track) -> int:
            return sum(f.box[0] <= px <= f.box[2] and f.box[1] <= py <= f.box[3] for f in t.faces.values())

        best = max(tracks, key=lambda t: (hits(t), len(t.faces)))
        if hits(best):
            return best
        near = min(tracks, key=lambda t: math.hypot(t.centre()[0] - px, t.centre()[1] - py))
        cx, cy = near.centre()
        return near if math.hypot(cx - px, cy - py) < 1.5 * near.height else None
    steady = [t for t in tracks if t.presence >= min_presence]
    if not steady:
        return None
    sharpest = max(t.sharpness for t in steady)
    sharp = [t for t in steady if t.sharpness >= 0.5 * sharpest]
    loudest = max(t.talk for t in sharp)
    return max(sharp, key=lambda t: (t.talk >= 0.8 * loudest, t.presence, t.talk))


# ---------------------------------------------------------------------------------------- per-frame speaker face

def fill_gaps(track: Track, frames: int, max_gap: int = 3) -> list[Face | None]:
    """The track's face in every frame: gaps of up to max_gap frames between two detections are interpolated,
    longer gaps (and frames before the first / after the last detection) are None."""
    out: list[Face | None] = [track.faces.get(i) for i in range(frames)]
    known = sorted(track.faces)
    for a, b in zip(known, known[1:]):
        if 1 < b - a <= max_gap + 1:
            fa, fb = track.faces[a], track.faces[b]
            for i in range(a + 1, b):
                t = (i - a) / (b - a)
                lerp = lambda u, v: [x + (y - x) * t for x, y in zip(u, v)]  # noqa: E731
                out[i] = Face(lerp(fa.box, fb.box), min(fa.score, fb.score),
                              [lerp(fa.eyes[0], fb.eyes[0]), lerp(fa.eyes[1], fb.eyes[1])],
                              lerp(fa.nose, fb.nose), lerp(fa.mouth, fb.mouth), track=track.id)
    return out


def mask_params(face: Face) -> tuple[float, float, float, float, float]:
    """Ellipse over the mouth and jaw, the area LatentSync redraws: centre x, centre y, half width, half height,
    angle (radians, the eye line's tilt). From just under the nose to below the chin, cheek to cheek."""
    d = face.scale()
    (lx, ly), (rx, ry) = face.eyes
    angle = math.atan2(ry - ly, rx - lx)
    ux, uy = -math.sin(angle), math.cos(angle)                 # "down" along the face
    nx, ny = face.nose
    mx, my = face.mouth
    top = (nx + ux * 0.12 * d, ny + uy * 0.12 * d)              # under the nose tip: the moustache is inside
    chin = (mx + ux * 0.9 * d, my + uy * 0.9 * d)               # below the chin, so a dropping jaw stays inside
    cx, cy = (top[0] + chin[0]) / 2, (top[1] + chin[1]) / 2
    half_h = max(math.hypot(chin[0] - top[0], chin[1] - top[1]) / 2, 0.35 * d)
    return cx, cy, 0.8 * d, half_h, angle


def smooth(params: list[tuple | None], window: int = 5) -> list[tuple | None]:
    """Moving average of each frame's ellipse over the neighbouring frames that have one (jitter-free mask)."""
    out: list[tuple | None] = []
    for i, p in enumerate(params):
        if p is None:
            out.append(None)
            continue
        near = [q for q in params[max(0, i - window // 2): i + window // 2 + 1] if q is not None]
        out.append(tuple(float(np.mean([q[k] for q in near])) for k in range(len(p))))
    return out


def ellipse_mask(params: tuple[float, float, float, float, float], width: int, height: int,
                 feather: float = 0.3) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """The feathered ellipse as float weights over its bounding region: (weights, (x0, y0, x1, y1)).
    1 inside the ellipse shrunk by `feather`, 0 outside it grown by `feather`, a smooth ramp in between."""
    cx, cy, a, b, ang = params
    reach = (1 + feather) * max(a, b)
    x0, y0 = max(0, int(cx - reach)), max(0, int(cy - reach))
    x1, y1 = min(width, int(cx + reach) + 1), min(height, int(cy + reach) + 1)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), np.float32), (0, 0, 0, 0)
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    dx, dy = xs - cx, ys - cy
    c, s = math.cos(ang), math.sin(ang)
    u, v = (dx * c + dy * s) / a, (-dx * s + dy * c) / b
    r = np.sqrt(u * u + v * v)                                  # 1 on the ellipse
    if feather <= 0:
        return (r <= 1).astype(np.float32), (x0, y0, x1, y1)
    w = np.clip((1 + feather - r) / (2 * feather), 0, 1)
    return (w * w * (3 - 2 * w)).astype(np.float32), (x0, y0, x1, y1)


# ------------------------------------------------------------------------------------------- checks and segments

def _box_blur(x: np.ndarray, k: int) -> np.ndarray:
    """Mean over a k x k window (k odd), same size as x."""
    c = np.cumsum(np.cumsum(np.pad(x, ((k // 2 + 1, k // 2), (k // 2 + 1, k // 2))), 0), 1)
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def edit_locations(original: Path, synced: Path, faces: list[Face | None]) -> list[tuple[float, float] | None]:
    """Where a synced clip changed each frame: (change on the speaker's mouth, strongest change away from the
    speaker's face), each the largest mean brightness change (0-255) over a window 0.4 face-heights wide. None where
    the speaker's face is unknown. LatentSync redraws a whole lower face, so a frame where it animated someone else
    shows a clearly bigger change away from the speaker than on the speaker's mouth; re-encoding alone gives ~2-5."""
    known = [f for f in faces if f is not None]
    if not known:
        return [None] * len(faces)
    k = int(max(9, 0.4 * float(np.median([f.height for f in known])))) | 1
    params = smooth([mask_params(f) if f else None for f in faces])
    out: list[tuple[float, float] | None] = []
    for i, (a, b) in enumerate(zip(read_frames(original), read_frames(synced))):
        f = faces[i] if i < len(faces) else None
        if f is None:
            out.append(None)
            continue
        h, w = a.shape[:2]
        change = _box_blur(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean(axis=2).astype(np.float32), k)
        m, (x0, y0, x1, y1) = ellipse_mask(params[i], w, h, feather=0.0)
        core = m > 0.5
        on = float(change[y0:y1, x0:x1][core].max()) if core.any() else 0.0
        pad = 0.6 * max(f.width, f.height)
        away = np.ones((h, w), bool)
        away[max(0, int(f.box[1] - pad)):max(0, int(f.box[3] + pad)),
             max(0, int(f.box[0] - pad)):max(0, int(f.box[2] + pad))] = False
        out.append((on, float(change[away].max()) if away.any() else 0.0))
    return out


def off_speaker_frames(locations: list[tuple[float, float] | None], keep: set[int], ratio: float = 1.5,
                       floor: float = 10.0) -> list[int]:
    """Frames where the synced clip changed something away from the speaker clearly more (ratio, and at least
    floor) than the speaker's mouth: LatentSync animated another face there. Frames kept as filmed are skipped."""
    return [i for i, loc in enumerate(locations)
            if loc is not None and i not in keep and loc[1] > max(ratio * loc[0], floor)]


@dataclass
class Segment:
    start: int                     # first frame
    end: int                       # one past the last frame
    window: tuple[int, int, int, int]   # x, y, width, height of the crop LatentSync sees (even sizes)


def plan_segments(faces: list[Face | None], width: int, height: int, keep: set[int] | None = None,
                  min_len: int = 8, margin: float = 0.7, min_size: int = 256) -> list[Segment]:
    """Runs of frames where the speaker's face is known (and not kept as filmed), each with one fixed crop window:
    the union of the speaker's boxes, grown by `margin` face widths on every side (LatentSync needs some context
    around the face), at least min_size square, inside the frame. Runs shorter than min_len frames are left as
    filmed."""
    keep = keep or set()
    runs, start = [], None
    for i in range(len(faces) + 1):
        on = i < len(faces) and faces[i] is not None and i not in keep
        if on and start is None:
            start = i
        elif not on and start is not None:
            runs.append((start, i))
            start = None
    out = []
    for a, b in runs:
        if b - a < min_len:
            continue
        boxes = np.array([faces[i].box for i in range(a, b)])
        fw = float(np.median(boxes[:, 2] - boxes[:, 0]))
        x0, y0 = boxes[:, 0].min() - margin * fw, boxes[:, 1].min() - margin * fw
        x1, y1 = boxes[:, 2].max() + margin * fw, boxes[:, 3].max() + margin * fw * 1.3   # more room for the chin
        cw, ch = max(x1 - x0, min_size), max(y1 - y0, min_size)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        cw, ch = min(cw, width), min(ch, height)
        x0 = int(min(max(cx - cw / 2, 0), width - cw))
        y0 = int(min(max(cy - ch / 2, 0), height - ch))
        cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2
        out.append(Segment(a, b, (x0, y0, cw, ch)))
    return out


def write_crop(original: Path, seg: Segment, blank: dict[int, list[list[float]]], speaker: list[Face | None],
               out: Path) -> Path:
    """Frames seg.start..seg.end-1 of the clip, cropped to seg.window, with every other face's box (grown by 15%)
    filled flat, except where it overlaps the speaker's own face, so LatentSync can only find the speaker."""
    x, y, w, h = seg.window
    _, _, fps = probe(original)
    with FrameWriter(out, w, h, fps) as wr:
        for i, rgb in enumerate(read_frames(original)):
            if i < seg.start:
                continue
            if i >= seg.end:
                break
            crop = rgb[y:y + h, x:x + w].copy()
            me = speaker[i]
            for box in blank.get(i, []):
                bw, bh = box[2] - box[0], box[3] - box[1]
                bx0, by0 = int(box[0] - 0.15 * bw) - x, int(box[1] - 0.15 * bh) - y
                bx1, by1 = int(box[2] + 0.15 * bw) - x, int(box[3] + 0.15 * bh) - y
                bx0, by0, bx1, by1 = max(bx0, 0), max(by0, 0), min(bx1, w), min(by1, h)
                if bx1 <= bx0 or by1 <= by0:
                    continue
                fill = np.ones((by1 - by0, bx1 - bx0), bool)
                if me is not None:                               # never cover the speaker's own face
                    sx0, sy0 = int(me.box[0]) - x - bx0, int(me.box[1]) - y - by0
                    sx1, sy1 = int(me.box[2]) - x - bx0, int(me.box[3]) - y - by0
                    fill[max(sy0, 0):max(sy1, 0), max(sx0, 0):max(sx1, 0)] = False
                region = crop[by0:by1, bx0:bx1]
                region[fill] = region.reshape(-1, 3).mean(axis=0).astype(np.uint8)
            wr.write(crop)
    return out


def cut_audio(audio: Path, start_s: float, duration_s: float, out: Path) -> Path:
    ffmpeg.run(["-i", str(audio), "-af", f"atrim=start={start_s:.6f}:duration={duration_s:.6f},asetpts=PTS-STARTPTS",
                str(out)])
    return out


# --------------------------------------------------------------------------------------------------- composite

@dataclass
class Plan:
    """How a shot's synced clip is put together. Synced pixels come from `full` (a whole-frame synced clip, frame for
    frame with the original) or from `segments` (synced crops, each frame for frame with its frames)."""
    faces: list[Face | None]
    full: Path | None = None
    segments: list[tuple[Segment, Path]] = field(default_factory=list)
    keep: set[int] = field(default_factory=set)              # frames left as filmed
    repair: list[tuple[int, int]] = field(default_factory=list)   # (a, b): frames between rebuilt from a and b
    fallback: Path | None = None   # a whole-frame result that missed the speaker in some frames: used if no crop comes
    protect: dict | None = None    # {"above": 185} / {"below": 40}: original pixels that bright (dark) stay as filmed


def _synced_frames(plan: Plan, frames: int) -> Iterator[np.ndarray | None]:
    """The synced picture for every frame (None: no synced pixels there), full-frame or as crops placed on None."""
    if plan.full is not None:
        it = read_frames(plan.full)
        for _ in range(frames):
            yield next(it, None)
        return
    segs = sorted(plan.segments, key=lambda s: s[0].start)
    k, it = 0, None
    for i in range(frames):
        while k < len(segs) and i >= segs[k][0].end:
            k, it = k + 1, None
        if k < len(segs) and segs[k][0].start <= i:
            if it is None:
                it = read_frames(segs[k][1])
            yield next(it, None)
        else:
            yield None


def restore_detail(orig: np.ndarray, synced: np.ndarray, face: Face, strength: float = 1.0,
                   tau: float = 6.0) -> np.ndarray:
    """LatentSync redraws the lower face through a VAE, which smooths stubble, moustache strands and skin texture.
    Put the original's fine detail back wherever the synced picture still has the original's shape there (cheeks,
    chin, moustache sides): the detail layer (picture minus its blur) is swapped in, weighted by how alike the two
    blurred pictures are. Never inside the lips and mouth opening, where the original's teeth would ghost.
    orig / synced: float32 regions of the same frame area; face: the speaker's face in that area's coordinates."""
    k = max(3, int(face.height / 60)) | 1
    lo_o = np.stack([_box_blur(_box_blur(orig[..., c], k), k) for c in range(3)], -1)
    lo_s = np.stack([_box_blur(_box_blur(synced[..., c], k), k) for c in range(3)], -1)
    alike = np.exp(-(np.abs(lo_o - lo_s).mean(axis=2) / tau) ** 2)
    d = face.scale()
    lips, (x0, y0, x1, y1) = ellipse_mask((face.mouth[0], face.mouth[1] + 0.05 * d, 0.5 * d, 0.3 * d,
                                           math.atan2(face.eyes[1][1] - face.eyes[0][1],
                                                      face.eyes[1][0] - face.eyes[0][0])),
                                          orig.shape[1], orig.shape[0], feather=0.5)
    weight = alike
    if lips.size:
        weight = alike.copy()
        weight[y0:y1, x0:x1] *= 1 - lips
    return synced + strength * weight[..., None] * ((orig - lo_o) - (synced - lo_s))


def protect_mask(orig: np.ndarray, protect: dict) -> np.ndarray:
    """Weights (0-1) of the pixels to keep as filmed: where the original is brighter than protect["above"] or darker
    than protect["below"] (0-255 luma), grown and softened by a few pixels. For a background next to the face (backlit
    water, night foliage) that LatentSync smeared into: skin is never that bright or dark there."""
    luma = orig[..., :3].astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)
    hit = np.zeros(luma.shape, bool)
    if protect.get("above") is not None:
        hit |= luma > float(protect["above"])
    if protect.get("below") is not None:
        hit |= luma < float(protect["below"])
    return np.clip(_box_blur(hit.astype(np.float32), 7) * 2.0, 0, 1)


def composite(original: Path, plan: Plan, out: Path, feather: float = 0.3, detail: float = 1.0) -> Path:
    """The original clip with the synced pixels pasted in only inside the speaker's feathered mouth ellipse. Frames
    in plan.keep (and frames without a speaker face or synced pixels) stay exactly as filmed. Repaired frames take
    their synced pixels blended from the two frames around them. detail: how much of the original's fine texture
    goes back onto the synced skin (restore_detail; 0 = off)."""
    w, h, fps = probe(original)
    n = sum(1 for _ in read_frames(original)) if plan.faces is None else len(plan.faces)
    params = smooth([mask_params(f) if f else None for f in plan.faces])
    ends = {i for a, b in plan.repair for i in (a, b)}
    held: dict[int, np.ndarray] = {}
    if plan.repair:                                              # the frames repairs are rebuilt from
        for i, (orig, syn) in enumerate(zip(read_frames(original), _synced_frames(plan, n))):
            if i in ends and syn is not None:
                held[i] = _place(orig, syn, plan, i).astype(np.float32)
    with FrameWriter(out, w, h, fps) as wr:
        for i, (orig, syn) in enumerate(zip(read_frames(original), _synced_frames(plan, n))):
            frame = orig
            p = params[i] if i < len(params) else None
            source = None
            rep = next(((a, b) for a, b in plan.repair if a < i < b), None)
            if rep and rep[0] in held and rep[1] in held:
                t = (i - rep[0]) / (rep[1] - rep[0])
                source = held[rep[0]] * (1 - t) + held[rep[1]] * t
            elif syn is not None:
                source = _place(orig, syn, plan, i).astype(np.float32)
            if p is not None and source is not None and i not in plan.keep:
                m, (x0, y0, x1, y1) = ellipse_mask(p, w, h, feather)
                if m.size:
                    frame = orig.copy()
                    o = orig[y0:y1, x0:x1].astype(np.float32)
                    syn = source[y0:y1, x0:x1]
                    if detail > 0 and plan.faces[i] is not None:
                        f = plan.faces[i]
                        shift = lambda q: [q[0] - x0, q[1] - y0]  # noqa: E731
                        local = Face([f.box[0] - x0, f.box[1] - y0, f.box[2] - x0, f.box[3] - y0], f.score,
                                     [shift(f.eyes[0]), shift(f.eyes[1])], shift(f.nose), shift(f.mouth))
                        syn = restore_detail(o, syn, local, detail)
                    if plan.protect:
                        m = m * (1 - protect_mask(o, plan.protect))
                    frame[y0:y1, x0:x1] = np.clip(o + m[..., None] * (syn - o) + 0.5, 0, 255)
            wr.write(frame)
    return out


def _place(orig: np.ndarray, syn: np.ndarray, plan: Plan, i: int) -> np.ndarray:
    """A synced frame at full size: as is, or its crop placed over the original at the segment's window."""
    if plan.full is not None:
        return syn
    seg = next(s for s, _ in plan.segments if s.start <= i < s.end)
    x, y, w, h = seg.window
    full = orig.copy()
    full[y:y + h, x:x + w] = syn[:h, :w]
    return full


def ranges(spec: Iterable[Iterable[int]] | None) -> set[int]:
    """[[a, b], ...] inclusive frame ranges as a set of frame numbers."""
    out: set[int] = set()
    for a, b in spec or []:
        out.update(range(int(a), int(b) + 1))
    return out


def review_sheet(original: Path, analysis: Analysis, speaker: Track | None, out: Path, picks: int = 3) -> Path:
    """Three frames with every face boxed (the speaker green, the rest red) and the mouth ellipse drawn, for a
    reviewer to confirm the speaker lock picked the right person."""
    from PIL import Image, ImageDraw

    n = analysis.frames
    wanted = [int(n * k / (picks + 1)) for k in range(1, picks + 1)]
    faces = fill_gaps(speaker, n) if speaker else [None] * n
    tiles = []
    for i, rgb in enumerate(read_frames(original)):
        if i not in wanted:
            continue
        img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(img)
        for t in analysis.tracks:
            f = t.faces.get(i)
            if f is not None:
                colour = (0, 220, 0) if speaker is not None and t.id == speaker.id else (230, 0, 0)
                draw.rectangle(f.box, outline=colour, width=5)
                draw.text((f.box[0], max(0, f.box[1] - 14)), f"t{t.id} talk {t.talk:.2f}", fill=colour)
        if faces[i] is not None:
            cx, cy, a, b, _ = mask_params(faces[i])
            draw.ellipse([cx - a, cy - b, cx + a, cy + b], outline=(255, 255, 0), width=3)
        tiles.append(img.resize((img.width // 3, img.height // 3)))
    sheet = Image.new("RGB", (sum(t.width for t in tiles) or 1, max((t.height for t in tiles), default=1)))
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += t.width
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=85)
    return out
