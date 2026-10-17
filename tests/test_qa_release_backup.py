"""The pure parts of the lip-sync QA, the film check, the delivery and the backup plan."""

import numpy as np
import pytest

from nraiorbit.ops import backup
from nraiorbit.post import ffmpeg
from nraiorbit.qa import film, lipsync
from nraiorbit.release import deliver, gcs

pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")


def test_runs_and_speech_mask():
    assert lipsync.runs([0, 1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 1], 3) == [(1, 3), (8, 11)]
    assert lipsync.runs([], 3) == []
    env = np.array([-120, -120, -20, -18, -25, -60, -120], float)
    assert lipsync.speech_mask(env).tolist() == [False, False, True, True, True, False, False]


def test_corr_lag_finds_the_shift():
    rng = np.random.default_rng(1)
    env = rng.normal(size=200)
    ap = np.roll(env, 3) + rng.normal(scale=0.05, size=200)    # the mouth follows the sound 3 frames later
    speech = np.ones(200, bool)
    c, lag = lipsync.corr_lag(ap, env, speech, lags=range(-6, 7))
    assert lag == 3 and c > 0.95
    assert np.isnan(lipsync.corr_lag(ap, env, np.zeros(200, bool))[0])


def test_block_diff_sees_a_blob_outside_the_face_and_ignores_codec_noise():
    rng = np.random.default_rng(2)
    a = rng.integers(0, 255, (180, 320, 3), dtype=np.uint8)
    noisy = np.clip(a.astype(int) + rng.integers(-2, 3, a.shape), 0, 255).astype(np.uint8)
    assert lipsync.block_diff(a, noisy) < 3
    blob = noisy.copy()
    blob[20:60, 250:300] = 255 - blob[20:60, 250:300]           # a smear far from the face
    assert lipsync.block_diff(a, blob, box=(100, 60, 180, 150)) > lipsync.BLOB_LEVEL
    assert lipsync.block_diff(a, blob, box=(230, 10, 320, 80)) < 3   # the same change inside the face box is not a blob


def test_envelope_and_picks(tmp_path):
    wav = tmp_path / "line.wav"
    ffmpeg.run(["-f", "lavfi", "-t", "2", "-i", "sine=f=220:r=48000", "-af", "volume='if(between(t,0.5,1.5),1,0)':eval=frame",
                str(wav)])
    env = lipsync.envelope_db(wav, 48)
    speech = lipsync.speech_mask(env)
    assert speech[0] is np.False_ and speech[24] and not speech[47]
    picks = lipsync.review_picks(env, speech, 48)
    onset = int(np.flatnonzero(speech)[0])
    assert 3 <= len(picks) <= 6 and onset in picks and onset + 3 in picks and min(picks) < onset   # a silent frame too


def test_verdict_names_what_stands_out():
    assert lipsync.verdict({"blob_max": 2.0, "closed_runs": [[3, 8]], "mesh_missing": 0, "frames": 100}) == []
    flags = lipsync.verdict({"blob_max": 14.0, "blob_frames": 3, "closed_runs": [[10, 40]], "mesh_missing": 60,
                             "frames": 100, "failed_segments": [{"why": "x"}]})
    assert len(flags) == 4 and "f10-40" in flags[1]


def test_film_lag_ms_measures_a_delay():
    rate = 16000
    t = np.arange(rate * 2) / rate
    ref = (np.sin(2 * np.pi * 220 * t) * np.exp(-((t - 0.7) ** 2) / 0.02)).astype(np.float32)
    late = np.concatenate([np.zeros(int(0.02 * rate), np.float32), ref])[: len(ref)]     # 20 ms late
    lag, corr = film.lag_ms(ref, late, rate)
    assert abs(lag - 20.0) < 1.0 and corr > 0.9
    assert np.isnan(film.lag_ms(ref, np.zeros_like(ref), rate)[0])


def test_delivery_names_and_720p_copy(tmp_path):
    nm = deliver.names("Godavari Simham", "2026-10-17", "voiceE")
    assert nm == {"master": "Godavari_Simham_2026-10-17_voiceE_1080p.mp4",
                  "small": "Godavari_Simham_2026-10-17_voiceE_720p.mp4",
                  "trailer": "Godavari_Simham_trailer_2026-10-17_voiceE.mp4"}
    master = tmp_path / "movie.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=blue:s=1920x1080:r=24:d=2", "-f", "lavfi", "-t", "2",
                "-i", "sine=f=330:r=48000", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(master)])
    small = deliver.encode_720p(master, tmp_path / "out" / "small.mp4")
    assert deliver.probe_width(small) == 1280
    parts = deliver.split_parts(master, tmp_path / "parts", 2)
    assert len(parts) == 2 and abs(parts[0][2] - 1.0) < 0.1 and all(p.exists() for p, _, _ in parts)


def test_deliver_without_upload_writes_the_record(tmp_path):
    root = tmp_path / "film"
    (root / "11_master").mkdir(parents=True)
    ffmpeg.run(["-f", "lavfi", "-i", "color=c=red:s=320x180:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(root / "11_master" / "movie.mp4")])
    recs = deliver.deliver(root, upload=False, date="2026-10-17", log=lambda *_: None)
    assert [r["name"] for r in recs] == ["film_2026-10-17_1080p.mp4", "film_2026-10-17_720p.mp4"]
    assert (root / "11_master" / "deliver" / "delivery.json").exists()


def test_backup_pairs_and_commands(tmp_path, monkeypatch):
    root = tmp_path / "godavari10"
    (root / "inbox").mkdir(parents=True)
    (root / "pipeline.yaml").write_text("x: 1")
    prs = backup.pairs(root, "my-media", ["inbox", "files"])
    assert prs[0] == ("folder", root / "inbox", "gs://my-media/godavari10/inbox")
    assert ("file", root / "pipeline.yaml", "gs://my-media/godavari10/project/pipeline.yaml") in prs
    with pytest.raises(ValueError):
        backup.pairs(root, "b", ["nope"])
    calls = []

    class Done:
        returncode = 0
        stderr = ""
    monkeypatch.setattr(gcs, "RUN", lambda cmd, **kw: calls.append(cmd) or Done())
    monkeypatch.setattr(gcs.shutil, "which", lambda name: "/usr/bin/gcloud")
    assert backup.backup(root, "my-media", ["inbox", "files"], log=lambda *_: None) == 2   # the missing files are skipped
    assert calls[0][:4] == ["gcloud", "storage", "rsync", "-r"] and calls[1][:3] == ["gcloud", "storage", "cp"]
