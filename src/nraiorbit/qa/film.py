"""The film check (`nraiorbit qa film <slug>`): the assembled master against the plan.

Frame count and length against the outline, integrated loudness and true peak, every speaking shot's voice placed
where its lip-sync track expects it (cross-correlation of the master's audio with the shot's lip-sync audio), and
the picture at a sample of cuts (the master's frame at each cut against the shot clip's first shown frame). Pure
measurement; nothing here spends money.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

FPS = 24
SR = 16000


def mono(path: Path, ss: float | None = None, t: float | None = None, rate: int = SR) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error"] + (["-ss", f"{ss:.6f}"] if ss is not None else []) + ["-i", str(path)] \
        + (["-t", f"{t:.6f}"] if t else []) + ["-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, np.int16).astype(np.float32) / 32768


def lag_ms(ref: np.ndarray, got: np.ndarray, rate: int = SR, max_ms: float = 100.0) -> tuple[float, float]:
    """(milliseconds `got` is late against `ref`, normalised correlation at that lag) within +-max_ms."""
    n = min(len(ref), len(got))
    ref, got = ref[:n], got[:n]
    if n < rate // 10 or np.abs(ref).max() < 1e-4 or np.abs(got).max() < 1e-4:
        return float("nan"), 0.0
    pad = 1 << int(np.ceil(np.log2(2 * n)))
    cc = np.fft.irfft(np.fft.rfft(got, pad) * np.conj(np.fft.rfft(ref, pad)), pad)
    w = int(max_ms / 1000 * rate)
    cand = np.concatenate([cc[-w:], cc[:w + 1]])
    lag = (int(np.argmax(cand)) - w) / rate * 1000
    peak = float(cand.max() / (np.linalg.norm(ref) * np.linalg.norm(got) + 1e-9))
    return lag, peak


def frame_at(path: Path, n: int, size=(320, 180)) -> np.ndarray | None:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{n})", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-"],
                         capture_output=True).stdout
    return np.frombuffer(out, np.uint8).reshape(size[1], size[0], 3).astype(np.int16) \
        if len(out) == size[0] * size[1] * 3 else None


def loudness(master: Path) -> dict:
    lu = subprocess.run(["ffmpeg", "-v", "info", "-nostats", "-i", str(master), "-af", "ebur128=peak=true",
                         "-f", "null", "-"], capture_output=True, text=True).stderr
    tail = lu[lu.rfind("Summary:"):]
    out = {}
    for key, name in (("I:", "lufs"), ("LRA:", "lra"), ("Peak:", "true_peak_dbtp")):
        i = tail.find(key)
        if i >= 0:
            try:
                out[name] = float(tail[i + len(key):i + 24].split()[0])
            except (ValueError, IndexError):
                pass
    return out


def check(project_root: Path, master: Path | None = None, fps: int = FPS, sample_cuts: int = 12, log=print) -> dict:
    project_root = Path(project_root)
    master = Path(master or project_root / "11_master" / "movie.mp4")
    tl = json.loads((project_root / "10_edit" / "timeline.json").read_text(encoding="utf-8"))
    shots = {s["shot_id"]: s for s in tl["shots"]}
    report: dict = {"master": str(master)}
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,nb_frames,duration",
                                      "-of", "json", str(master)], capture_output=True, text=True).stdout)
    for st in info.get("streams", []):
        if st["codec_type"] == "video":
            report["frames"] = int(st.get("nb_frames") or 0)
        report[f"{st['codec_type']}_seconds"] = float(st.get("duration") or 0)
    report["expected_frames"] = int(round(tl["film_seconds"] * fps))
    report.update(loudness(master))
    log(f"  frames {report.get('frames')} of {report['expected_frames']} expected; "
        f"{report.get('lufs', float('nan')):.1f} LUFS, true peak {report.get('true_peak_dbtp', float('nan')):.1f} dBTP")
    # voice placement per speaking shot
    rows = {}
    ls = project_root / "reports" / "lipsync.jsonl"
    if ls.exists():
        for line in ls.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["shot_id"]] = r
    lags = {}
    for sid, r in sorted(rows.items()):
        s = shots.get(sid)
        clip = Path(r["clip"])
        wav = clip.with_name(clip.stem + ".lipsync.wav")
        if s is None or not wav.exists():
            continue
        origin, length = s["start_s"] - s.get("in_s", 0.0), r["speaker"]["of"] / fps
        lag, peak = lag_ms(mono(wav), mono(master, origin, length))
        if not np.isnan(lag):
            lags[sid] = {"lag_ms": round(lag, 1), "corr": round(peak, 2)}
    report["voice_placement"] = lags
    if lags:
        vals = [v["lag_ms"] for v in lags.values()]
        report["voice_lag_max_ms"], report["voice_lag_median_ms"] = max(abs(v) for v in vals), float(np.median(vals))
        log(f"  voice placement: {len(lags)} shots, median {report['voice_lag_median_ms']:.1f} ms, "
            f"max {report['voice_lag_max_ms']:.1f} ms late against the lip-sync tracks")
    # cuts on the frame grid
    order = tl["shots"]
    sample = order[1:len(order):max(1, len(order) // sample_cuts)][:sample_cuts]
    cuts = []
    for s in sample:
        n0 = int(round(s["start_s"] * fps))
        first = int(round(s.get("in_s", 0.0) * fps))
        m, c = frame_at(master, n0), frame_at(Path(s["clip"]), first)
        prev = frame_at(master, n0 - 1)
        if m is None or c is None:
            continue
        cuts.append({"shot": s["shot_id"], "frame": n0, "diff_at_cut": round(float(np.abs(m - c).mean()), 1),
                     "diff_frame_before": round(float(np.abs(prev - c).mean()), 1) if prev is not None else None})
    report["cuts"] = cuts
    if cuts:
        worst = max(c["diff_at_cut"] for c in cuts)
        report["cut_worst_diff"] = worst
        log(f"  cuts: {len(cuts)} sampled, worst difference at the cut {worst:.1f} (under 6 is the same frame)")
    report["ok"] = (report.get("frames") == report["expected_frames"]
                    and report.get("cut_worst_diff", 0) < 6
                    and report.get("voice_lag_max_ms", 0) <= 1000 / fps / 2)
    return report
