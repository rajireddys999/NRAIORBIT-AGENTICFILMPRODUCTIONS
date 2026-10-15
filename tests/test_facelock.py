"""Lip-sync speaker lock: the speaker's face is found, LatentSync sees only it, and only its mouth area changes."""

import numpy as np
import pytest

from nraiorbit.post import facelock as fl
from nraiorbit.post import ffmpeg

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")

W, H, FPS, N = 320, 192, 24, 24
TALKER, STILL, BLURRED = (40, 40, 64), (200, 50, 56), (110, 100, 88)     # x0, y0, size


def _face(x0, y0, s):
    return fl.Face([x0, y0, x0 + s, y0 + s], 0.9, [[x0 + 0.3 * s, y0 + 0.35 * s], [x0 + 0.7 * s, y0 + 0.35 * s]],
                   [x0 + 0.5 * s, y0 + 0.55 * s], [x0 + 0.5 * s, y0 + 0.75 * s])


class FakeDetector:
    """Reports faces where the synthetic clip has them (present(i, k) can hide face k in frame i)."""

    def __init__(self, specs, present=lambda i, k: True):
        self.specs, self.present, self.calls = specs, present, 0

    def __call__(self, rgb):
        i, self.calls = self.calls, self.calls + 1
        return [_face(*s) for k, s in enumerate(self.specs) if self.present(i, k)]


def _blur(x, k=9):
    ker = np.ones(k) / k
    x = np.apply_along_axis(lambda r: np.convolve(r, ker, "same"), 0, x.astype(float))
    return np.apply_along_axis(lambda r: np.convolve(r, ker, "same"), 1, x).astype(np.uint8)


def _clip(path, specs=(TALKER, STILL, BLURRED), talking=(0, 2), blurred=(2,), n=N, seed=0):
    """Smooth background; each face a textured square (blurred faces much softer); a talking face's mouth, a dark
    bar, opens and closes from frame to frame."""
    rng = np.random.default_rng(seed)
    base = np.stack([_blur(rng.integers(60, 200, (H, W)), 15) for _ in range(3)], -1)
    tex = []
    for k, (_, _, s) in enumerate(specs):
        t = rng.integers(0, 255, (s, s, 3))
        tex.append(np.stack([_blur(t[..., c], 11 if k in blurred else 3) for c in range(3)], -1))
    with fl.FrameWriter(path, W, H, FPS) as w:
        for _ in range(n):
            f = base.copy()
            for k, (x0, y0, s) in enumerate(specs):
                f[y0:y0 + s, x0:x0 + s] = tex[k]
                if k in talking:
                    my, mx, hw = y0 + int(0.75 * s), x0 + int(0.5 * s), int(0.22 * s)
                    hh = int(rng.integers(1, int(0.12 * s) + 1))
                    f[my - hh:my + hh, mx - hw:mx + hw] = 20
            w.write(f)
    return path


def _frames(path):
    return np.stack(list(fl.read_frames(path))).astype(np.int16)


def test_analyse_and_pick_the_sharp_talking_face(tmp_path):
    clip = _clip(tmp_path / "shot.mp4")
    det = FakeDetector([TALKER, STILL, BLURRED])
    a = fl.analyse(clip, det, cache_dir=tmp_path / "faces")
    assert (a.width, a.height, a.frames) == (W, H, N) and len(a.tracks) == 3
    talker, still, blurred = sorted(a.tracks, key=lambda t: t.faces[0].box[0] if t.faces[0].box[0] != 110 else 1e9)
    assert all(t.presence == 1.0 for t in a.tracks)
    assert talker.talk > 3 * still.talk                           # the speaker's mouth moves, the listener's does not
    assert blurred.sharpness < 0.5 * talker.sharpness             # the out-of-focus face is soft...
    assert blurred.talk > still.talk                              # ...and talks, but is never the speaker
    assert fl.pick_speaker(a).id == talker.id
    hint = [(STILL[0] + STILL[2] / 2) / W, (STILL[1] + STILL[2] / 2) / H]
    assert fl.pick_speaker(a, hint=hint).id == still.id           # a point from the config wins
    assert fl.pick_speaker(a, hint=[0.02, 0.98]) is None          # a point on no face picks nobody
    calls = det.calls
    again = fl.analyse(clip, det, cache_dir=tmp_path / "faces")
    assert det.calls == calls and [t.talk for t in again.tracks] == [t.talk for t in a.tracks]   # from the cache


def test_fill_gaps_bridges_only_short_gaps():
    t = fl.Track(0, {i: _face(10 + i, 20, 50) for i in (0, 1, 2, 5, 10)})
    faces = fl.fill_gaps(t, 12, max_gap=3)
    assert faces[3].box[0] == pytest.approx(13) and faces[4].box[0] == pytest.approx(14)   # interpolated
    assert all(faces[i] is None for i in (6, 7, 8, 9, 11))         # a 4-frame gap and the tail stay unknown


def test_plan_segments_split_where_the_face_is_missing_or_kept():
    faces = [None if 10 <= i < 14 else _face(100, 40, 60) for i in range(30)]
    segs = fl.plan_segments(faces, W, H, keep=set(range(24, 30)))
    assert [(s.start, s.end) for s in segs] == [(0, 10), (14, 24)]
    for s in segs:
        x, y, w, h = s.window
        assert x >= 0 and y >= 0 and x + w <= W and y + h <= H and w % 2 == 0 and h % 2 == 0
        assert x <= 100 and x + w >= 160 and y <= 40 and y + h >= 100       # the whole face is inside
    assert fl.plan_segments(faces[:12], W, H, min_len=12) == []            # runs shorter than min_len are skipped


def test_crop_blanks_other_faces_but_never_the_speaker(tmp_path):
    clip = _clip(tmp_path / "shot.mp4", specs=(TALKER, (120, 60, 56)), talking=(0,), blurred=())
    speaker = [_face(*TALKER)] * N
    seg = fl.Segment(0, N, (0, 0, 256, 192))
    out = fl.write_crop(clip, seg, {i: [_face(120, 60, 56).box] for i in range(N)}, speaker, tmp_path / "crop.mp4")
    f = _frames(out)
    assert f.shape[1:3] == (192, 256)
    other = f[:, 64:112, 128:168]                                  # inside the other face: flat
    me = f[:, 45:100, 45:100]                                      # the speaker: untouched texture
    assert other.std(axis=(1, 2)).max() < 6 and me.std(axis=(1, 2)).min() > 15


def test_wrong_face_frames_are_found_and_only_the_speaker_mouth_changes(tmp_path):
    clip = _clip(tmp_path / "shot.mp4", specs=(TALKER, STILL), talking=(0,), blurred=())
    orig = _frames(clip)
    # a whole-frame "LatentSync" result: frames 0-11 redraw the speaker's mouth, frames 12-23 the other face's
    speaker = fl.fill_gaps(fl.Track(0, {i: _face(*TALKER) for i in range(N)}), N)
    wrong = fl.fill_gaps(fl.Track(1, {i: _face(*STILL) for i in range(N)}), N)
    with fl.FrameWriter(tmp_path / "synced.mp4", W, H, FPS) as w:
        for i, f in enumerate(orig.astype(np.uint8)):
            f = f.copy()
            face = speaker[i] if i < 12 else wrong[i]
            m, (x0, y0, x1, y1) = fl.ellipse_mask(fl.mask_params(face), W, H, feather=0.0)
            region = f[y0:y1, x0:x1]
            region[m > 0.5] = 255 - region[m > 0.5]
            f[:, :8] = 0                                            # and something outside every face
            w.write(f)
    synced = tmp_path / "synced.mp4"
    where = fl.edit_locations(clip, synced, speaker)
    assert min(on for on, _ in where[:12]) > 30 and max(on for on, _ in where[12:]) < 6   # on the speaker's mouth
    assert min(away for _, away in where[12:]) > 30                     # ...or on the other face instead
    assert fl.off_speaker_frames(where, keep=set()) == list(range(12, N))   # the edge strip never counts
    assert fl.off_speaker_frames(where, keep=set(range(12, N))) == []

    plan = fl.Plan(speaker, full=synced, keep={0, 1})
    out = _frames(fl.composite(clip, plan, tmp_path / "out.mp4"))
    diff = np.abs(out - orig).mean(axis=3)
    m, (x0, y0, x1, y1) = fl.ellipse_mask(fl.smooth([fl.mask_params(speaker[5])])[0], W, H, feather=0.3)
    outside = np.ones((H, W), bool)
    outside[y0:y1, x0:x1] = m == 0
    assert diff[:, outside].max(axis=1).max() < 40 and np.percentile(diff[:, outside], 99) < 6   # nothing else moves
    assert diff[0:2].max() < 40 and np.percentile(diff[0:2], 99) < 6                             # kept as filmed
    core = np.zeros((H, W), bool)
    core[y0:y1, x0:x1] = m > 0.9
    assert diff[2:12][:, core].mean() > 60                                                     # the synced mouth
    assert diff[12:][:, core].mean() < 6                       # the wrong-face frames bring nothing onto the speaker
    sx0, sy0, s = STILL
    assert diff[:, sy0:sy0 + s, sx0:sx0 + s].mean() < 4        # the other face never changes


def test_crops_are_pasted_back_through_the_mask_and_repairs_blend(tmp_path):
    clip = _clip(tmp_path / "shot.mp4", specs=(TALKER, STILL), talking=(0,), blurred=())
    orig = _frames(clip)
    speaker = [_face(*TALKER)] * N
    seg = fl.Segment(4, 20, (0, 0, 256, 192))
    with fl.FrameWriter(tmp_path / "crop_synced.mp4", 256, 192, FPS) as w:     # the synced crop: all white
        for _ in range(seg.end - seg.start):
            w.write(np.full((192, 256, 3), 255, np.uint8))
    plan = fl.Plan(speaker, segments=[(seg, tmp_path / "crop_synced.mp4")], repair=[(8, 12)])
    out = _frames(fl.composite(clip, plan, tmp_path / "out.mp4"))
    cx, cy = int(TALKER[0] + 0.5 * TALKER[2]), int(TALKER[1] + 0.8 * TALKER[2])
    mouth = (slice(None), slice(cy - 3, cy + 4), slice(cx - 3, cx + 4))
    o, b = out[mouth], orig[mouth]
    assert (o[4:20].mean(axis=(1, 2, 3)) > 235).all()                      # inside the segment: the synced crop
    assert (np.abs(o[:4] - b[:4]).mean(axis=(1, 2, 3)) < 8).all()          # before and after it: as filmed
    assert (np.abs(o[20:] - b[20:]).mean(axis=(1, 2, 3)) < 8).all()
    assert np.abs(out[:, 0:20, 200:300] - orig[:, 0:20, 200:300]).mean() < 4   # crop pixels outside the mask: unused
    assert (o[9:12].mean(axis=(1, 2, 3)) > 235).all()                      # repaired frames: blended from 8 and 12


def test_restore_detail_brings_back_texture_but_not_the_old_mouth():
    rng = np.random.default_rng(3)
    size = 160
    face = _face(20, 10, 120)
    orig = np.full((size, size, 3), 120.0, np.float32) + rng.normal(0, 25, (size, size, 3)).astype(np.float32)
    soft = np.stack([fl._box_blur(fl._box_blur(orig[..., c], 3), 3) for c in range(3)], -1)   # LatentSync VAE
    synced = soft.copy()
    mx, my = int(face.mouth[0]), int(face.mouth[1])
    synced[my - 8:my + 8, mx - 25:mx + 25] = 20.0                    # a new, open mouth
    out = fl.restore_detail(orig, synced, face)

    def texture(x, ys, xs):
        g = x[ys, xs].mean(axis=2)
        return float((g[1:-1, 1:-1] * 4 - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]).var())

    chin = (slice(my + 30, my + 50), slice(mx - 30, mx + 30))
    assert texture(soft, *chin) < 0.2 * texture(orig, *chin)         # the synced chin is airbrushed...
    assert texture(out, *chin) > 0.7 * texture(orig, *chin)          # ...and gets its stubble back
    mouth = (slice(my - 6, my + 6), slice(mx - 20, mx + 20))
    assert abs(out[mouth].mean() - 20.0) < 3 and texture(out, *mouth) < 0.1 * texture(orig, *mouth)   # the new mouth
