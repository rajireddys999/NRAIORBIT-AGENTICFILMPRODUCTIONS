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


def test_protect_keeps_a_bright_background_beside_the_mouth(tmp_path):
    """LatentSync smeared skin into the backlit water beside a turned face: protect: {above: 185} keeps the water."""
    rng = np.random.default_rng(5)
    face = _face(*TALKER)
    x0, y0, s = TALKER
    edge = x0 + 46                                                    # the turned face ends before the patch does
    base = np.full((H, W, 3), 230, np.uint8)                          # bright backlit water...
    base[y0:y0 + s, x0:edge] = np.clip(rng.normal(110, 12, (s, edge - x0, 3)), 0, 255).astype(np.uint8)   # ...skin
    with fl.FrameWriter(tmp_path / "orig.mp4", W, H, FPS) as w:
        for _ in range(4):
            w.write(base)
    syn = base.copy()
    mx, my = int(face.mouth[0]), int(face.mouth[1])
    syn[my - 3:my + 3, mx - 8:mx + 6] = 20                           # the new mouth, on skin
    syn[my - 6:my + 8, edge:edge + 10] = 150                         # a skin-coloured smear into the water
    with fl.FrameWriter(tmp_path / "syn.mp4", W, H, FPS) as w:
        for _ in range(4):
            w.write(syn)
    faces = [face] * 4
    plain = _frames(fl.composite(tmp_path / "orig.mp4", fl.Plan(faces, full=tmp_path / "syn.mp4"), tmp_path / "a.mp4",
                                 detail=0))
    guarded = _frames(fl.composite(tmp_path / "orig.mp4", fl.Plan(faces, full=tmp_path / "syn.mp4",
                                                                  protect={"above": 185}), tmp_path / "b.mp4", detail=0))
    smear = (slice(None), slice(my - 4, my + 6), slice(edge + 3, edge + 9))
    assert plain[smear].mean() < 200                                  # without the guard the smear shows
    assert guarded[smear].mean() > 220                                # with it, the water is as filmed
    mouth = (slice(None), slice(my - 2, my + 2), slice(mx - 6, mx + 4))
    assert guarded[mouth].mean() < 60                                 # the new mouth is still there


def _mesh(x0, y0, x1, y1, mouth):
    """Synthetic face-mesh points (MESH_POINTS order): the oval inside the box, the lips around `mouth`."""
    t = np.linspace(0, 2 * np.pi, 36, endpoint=False)
    cx, cy, a, b = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    oval = np.stack([cx + a * np.sin(t), cy - b * np.cos(t)], 1)
    u = np.linspace(0, 2 * np.pi, 20, endpoint=False)
    lips = np.stack([mouth[0] + 9 * np.cos(u), mouth[1] + 4 * np.sin(u)], 1)
    inner = np.stack([mouth[0] + 6 * np.cos(u), mouth[1] + 2 * np.sin(u)], 1)
    nose = np.tile([mouth[0], mouth[1] - 12], (12, 1)) + np.linspace(0, 1, 12)[:, None]
    eyes = np.array([[x0 + 8, y0 + 20], [x0 + 16, y0 + 20], [x1 - 16, y0 + 20], [x1 - 8, y0 + 20]], float)
    return np.vstack([oval, lips, inner, nose, eyes]).astype(np.float32)


def test_mesh_clip_keeps_the_mouth_and_drops_a_smear_beside_the_face(tmp_path):
    """A turned face whose skin is no brighter or darker than the wall behind it (so protect cannot tell them apart):
    the face-mesh outline keeps LatentSync's smear on the wall out and its new mouth in."""
    rng = np.random.default_rng(7)
    x0, y0, s = TALKER
    edge = x0 + 46                                                    # the face ends before the patch does
    face = _face(*TALKER)
    mx, my = int(face.mouth[0]), int(face.mouth[1])
    base = np.clip(rng.normal(120, 6, (H, W, 3)), 0, 255).astype(np.uint8)
    with fl.FrameWriter(tmp_path / "orig.mp4", W, H, FPS) as w:
        for _ in range(4):
            w.write(base)
    syn = base.copy()
    syn[my - 3:my + 3, mx - 8:mx + 6] = 20                           # the new mouth
    syn[my - 6:my + 10, edge + 2:edge + 10] = 200                    # a smear on the wall beside the face
    with fl.FrameWriter(tmp_path / "syn.mp4", W, H, FPS) as w:
        for _ in range(4):
            w.write(syn)
    mesh = _mesh(x0 + 2, y0 + 2, edge, y0 + s - 2, (mx, my))
    faces, meshes = fl.mesh_faces([face] * 4, [mesh] * 4)
    assert abs(faces[0].mouth[0] - mx) < 1 and abs(faces[0].mouth[1] - my) < 1
    plain = _frames(fl.composite(tmp_path / "orig.mp4", fl.Plan([face] * 4, full=tmp_path / "syn.mp4"),
                                 tmp_path / "a.mp4", detail=0))                    # the detector's face, no mesh
    clipped = _frames(fl.composite(tmp_path / "orig.mp4", fl.Plan(faces, full=tmp_path / "syn.mp4", meshes=meshes),
                                   tmp_path / "b.mp4", detail=0))
    smear = (slice(None), slice(my - 4, my + 8), slice(edge + 3, edge + 8))
    assert plain[smear].mean() > 150                                  # without the outline the smear shows
    assert abs(clipped[smear].mean() - 120) < 8                       # with it, the wall is as filmed
    mouth = (slice(None), slice(my - 2, my + 2), slice(mx - 6, mx + 4))
    assert clipped[mouth].mean() < 50                                 # the new mouth is there


def test_mesh_faces_follow_the_lips_and_bridge_short_mesh_gaps():
    face = _face(*TALKER)                                             # the detector's mouth...
    lips_at = (face.mouth[0] + 15, face.mouth[1] - 5)                # ...is 15 px off the real lips
    m = _mesh(40, 40, 104, 104, lips_at)
    points = [m, None, m + 4, None, None, None, None, None, None, None, None, m]
    faces, meshes = fl.mesh_faces([face] * 12, points, max_gap=3, window=1)
    assert faces[0].mouth == pytest.approx(list(lips_at), abs=0.5)
    assert meshes[1] is not None and np.allclose(meshes[1], m + 2)  # a one-frame gap: interpolated
    assert meshes[5] is None and faces[5] is face                    # a long gap: the detector's face, no outline
    assert len(fl._hull(np.array([[0, 0], [4, 0], [4, 4], [0, 4], [2, 2]]))) == 4


def test_a_bridged_face_gap_takes_the_whole_frame_result_and_fades_into_kept_frames(tmp_path):
    clip = _clip(tmp_path / "shot.mp4", specs=(TALKER, STILL), talking=(0,), blurred=())
    orig = _frames(clip)
    speaker = [_face(*TALKER)] * N
    crops = []
    for k, (a, b) in enumerate([(0, 10), (15, N)]):
        path = tmp_path / f"crop{k}.mp4"
        with fl.FrameWriter(path, 256, 192, FPS) as w:                # the synced crops: all white
            for _ in range(b - a):
                w.write(np.full((192, 256, 3), 255, np.uint8))
        crops.append((fl.Segment(a, b, (0, 0, 256, 192)), path))
    with fl.FrameWriter(tmp_path / "whole.mp4", W, H, FPS) as w:      # the whole-frame result: mid grey
        for _ in range(N):
            w.write(np.full((H, W, 3), 128, np.uint8))
    cx, cy = int(TALKER[0] + 0.5 * TALKER[2]), int(TALKER[1] + 0.8 * TALKER[2])
    mouth = (slice(None), slice(cy - 3, cy + 4), slice(cx - 3, cx + 4))
    plan = fl.Plan(speaker, segments=crops, fallback=tmp_path / "whole.mp4", fill={10, 11, 12, 13, 14})
    o = _frames(fl.composite(clip, plan, tmp_path / "out.mp4", detail=0))[mouth].mean(axis=(1, 2, 3))
    assert (o[:10] > 235).all() and (o[15:] > 235).all()              # the crops
    assert (np.abs(o[10:15] - 128) < 8).all()                         # the gap: the whole-frame mouth
    assert fl.fade_gains(10, {0, 1, 2}, 2) == pytest.approx([1, 1, 1, 1 / 3, 2 / 3, 1, 1, 1, 1, 1])
    faded = fl.Plan(speaker, segments=crops[1:], keep=set(range(15, 18)), fade=2)
    f = _frames(fl.composite(clip, faded, tmp_path / "faded.mp4", detail=0))[mouth].mean(axis=(1, 2, 3))
    b = orig[mouth].mean(axis=(1, 2, 3))
    assert (np.abs(f[15:18] - b[15:18]) < 8).all()                    # kept as filmed
    for i, g in ((18, 1 / 3), (19, 2 / 3), (21, 1)):                  # then the synced mouth fades in
        assert f[i] == pytest.approx(b[i] + g * (255 - b[i]), abs=12)


def test_extend_track_takes_over_a_split_detection_of_the_same_face():
    a = fl.Track(0, {i: _face(40, 40, 64) for i in range(0, 10)})
    b = fl.Track(1, {i: _face(42, 41, 64) for i in range(7, 20)})     # the same face, split off: overlaps at 7-9
    c = fl.Track(2, {i: _face(200, 50, 56) for i in range(0, 20)})    # someone else
    d = fl.Track(3, {i: _face(41, 40, 64) for i in range(25, 30)})    # the same place later, not touching: not taken
    out = fl.extend_track(a, [a, b, c, d])
    assert sorted(out.faces) == list(range(20)) and out.faces[15] is b.faces[15] and out.faces[5] is a.faces[5]
    assert sorted(fl.extend_track(b, [a, b, c, d]).faces) == list(range(20))      # backwards as well
    assert sorted(fl.extend_track(c, [a, b, c, d]).faces) == list(range(20))      # nothing of anyone else's
