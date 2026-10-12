"""Voice guard: right speaker, right words, right emotion, natural quality.

Measurements come from adapters (speaker embedder, Telugu ASR, speech-emotion model,
MOS predictor). This module turns them into pass/fail plus a ranking score for
best-of-N take selection. See docs/VOICE_TELUGU.md.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

import numpy as np


def _graphemes(text: str) -> list[str]:
    """Approximate grapheme clusters: base char + following combining marks.

    Telugu vowel signs (matras) and the virama are combining marks, so a consonant
    with its matra counts as one unit. That makes CER meaningful for Telugu script.
    """
    text = unicodedata.normalize("NFC", text)
    clusters: list[str] = []
    for ch in text:
        if ch.isspace() or unicodedata.category(ch).startswith("P"):
            continue
        if clusters and (unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Mc")):
            clusters[-1] += ch
        elif clusters and clusters[-1].endswith("్"):   # virama joins the next consonant (conjunct)
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def cer(reference: str, hypothesis: str) -> float:
    """Character (grapheme) error rate via Levenshtein distance."""
    ref, hyp = _graphemes(reference), _graphemes(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / len(ref)


@dataclass
class VoiceTake:
    take_id: str
    speaker_embedding: np.ndarray | None = None
    asr_text: str | None = None
    emotion_top2: list[str] = field(default_factory=list)
    mos: float | None = None
    duration_s: float | None = None
    peak_dbfs: float | None = None


@dataclass
class VoiceThresholds:
    min_speaker_similarity: float = 0.70
    max_cer: float = 0.15
    min_mos: float = 3.5
    duration_tolerance: float = 0.20

    @classmethod
    def from_config(cls, cfg: dict) -> "VoiceThresholds":
        return cls(**{k: v for k, v in cfg.items() if k in cls.__dataclass_fields__})


@dataclass
class VoiceResult:
    take_id: str
    passed: bool
    score: float
    issues: list[str]
    metrics: dict


class VoiceGuard:
    def __init__(self, speaker_centroids: dict[str, np.ndarray], thresholds: VoiceThresholds):
        self.centroids = {k: v / np.linalg.norm(v) for k, v in speaker_centroids.items()}
        self.t = thresholds

    def check(self, take: VoiceTake, *, voice_id: str, script_te: str, target_rasa: str,
              target_duration_s: float | None = None) -> VoiceResult:
        issues, metrics, parts = [], {}, []

        if take.speaker_embedding is not None and voice_id in self.centroids:
            e = take.speaker_embedding / np.linalg.norm(take.speaker_embedding)
            sim = float(e @ self.centroids[voice_id])
            metrics["speaker_similarity"] = round(sim, 4)
            parts.append(sim)
            if sim < self.t.min_speaker_similarity:
                issues.append(f"speaker similarity {sim:.3f} < {self.t.min_speaker_similarity}")

        if take.asr_text is not None:
            err = cer(script_te, take.asr_text)
            metrics["cer"] = round(err, 4)
            parts.append(1 - min(err, 1.0))
            if err > self.t.max_cer:
                issues.append(f"CER {err:.3f} > {self.t.max_cer} (words differ from script)")

        if take.emotion_top2:
            hit = target_rasa in take.emotion_top2
            metrics["emotion_match"] = hit
            parts.append(1.0 if hit else 0.0)
            if not hit:
                issues.append(f"emotion {take.emotion_top2} does not include target '{target_rasa}'")

        if take.mos is not None:
            metrics["mos"] = take.mos
            parts.append(take.mos / 5)
            if take.mos < self.t.min_mos:
                issues.append(f"MOS {take.mos:.2f} < {self.t.min_mos}")

        if take.duration_s is not None and target_duration_s:
            ratio = take.duration_s / target_duration_s
            metrics["duration_ratio"] = round(ratio, 3)
            if abs(ratio - 1) > self.t.duration_tolerance:
                issues.append(f"duration {take.duration_s:.2f}s vs target {target_duration_s:.2f}s")

        if take.peak_dbfs is not None and take.peak_dbfs >= -0.1:
            issues.append("clipping")

        score = float(np.mean(parts)) if parts else 0.0
        return VoiceResult(take.take_id, not issues, round(score, 4), issues, metrics)

    def select_best(self, results: list[VoiceResult]) -> VoiceResult | None:
        passing = [r for r in results if r.passed]
        return max(passing, key=lambda r: r.score) if passing else None
