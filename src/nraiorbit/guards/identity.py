"""Character identity guard: is every expected character's face (and body) the locked one?

Model-agnostic: a FaceEmbedder adapter (InsightFace/ArcFace, a commercial face API, ...)
turns frames into FaceObservations; this module does assignment, thresholds and drift
detection. See docs/CHARACTER_IDENTITY.md.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np


@dataclass
class FaceObservation:
    embedding: np.ndarray            # L2-normalised
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels
    det_score: float = 1.0

    @property
    def size_px(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return min(x2 - x1, y2 - y1)


class FaceEmbedder(Protocol):
    def detect_and_embed(self, image_path: Path) -> list[FaceObservation]: ...


@dataclass
class IdentityThresholds:
    min_mean_similarity: float = 0.55
    min_frame_similarity: float = 0.40
    max_similarity_std: float = 0.08
    min_face_px: float = 48

    @classmethod
    def for_shot(cls, guard_cfg: dict, shot_size: str, overrides: dict | None = None) -> "IdentityThresholds":
        mean_by_size = guard_cfg.get("min_mean_similarity", {})
        t = cls(
            min_mean_similarity=mean_by_size.get(shot_size, mean_by_size.get("MS", 0.45)),
            min_frame_similarity=guard_cfg.get("min_frame_similarity", 0.40),
            max_similarity_std=guard_cfg.get("max_similarity_std", 0.08),
            min_face_px=guard_cfg.get("min_face_px", 48),
        )
        for key, value in (overrides or {}).items():
            if hasattr(t, key):
                setattr(t, key, value)
        return t


@dataclass
class CharacterScore:
    character_id: str
    similarities: list[float] = field(default_factory=list)   # per frame where matched
    frames_missing: int = 0
    swapped_frames: int = 0

    @property
    def mean(self) -> float:
        return float(np.mean(self.similarities)) if self.similarities else 0.0

    @property
    def minimum(self) -> float:
        return float(np.min(self.similarities)) if self.similarities else 0.0

    @property
    def std(self) -> float:
        return float(np.std(self.similarities)) if len(self.similarities) > 1 else 0.0


@dataclass
class IdentityResult:
    passed: bool
    scores: dict[str, CharacterScore]
    issues: list[str]
    unknown_faces: int

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "unknown_faces": self.unknown_faces,
            "scores": {
                cid: {"mean": round(s.mean, 4), "min": round(s.minimum, 4), "std": round(s.std, 4),
                      "frames_missing": s.frames_missing, "swapped_frames": s.swapped_frames}
                for cid, s in self.scores.items()
            },
        }


def _normalise(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def build_centroid(embeddings: list[np.ndarray]) -> np.ndarray:
    """Face centroid for an identity pack: mean of L2-normalised embeddings, renormalised."""
    return _normalise(np.mean([_normalise(e) for e in embeddings], axis=0))


def assign_faces(sim: np.ndarray) -> list[tuple[int, int]]:
    """Optimal one-to-one assignment (face_idx, char_idx) maximising total similarity.

    Exhaustive for the small matrices a shot produces; greedy beyond 7 faces.
    """
    n_faces, n_chars = sim.shape
    if n_faces == 0 or n_chars == 0:
        return []
    if max(n_faces, n_chars) > 7:
        pairs, used_f, used_c = [], set(), set()
        for f, c in sorted(np.ndindex(sim.shape), key=lambda fc: -sim[fc]):
            if f not in used_f and c not in used_c:
                pairs.append((f, c))
                used_f.add(f)
                used_c.add(c)
        return pairs
    best, best_score = [], -np.inf
    if n_faces >= n_chars:
        for faces in itertools.permutations(range(n_faces), n_chars):
            score = sum(sim[f, c] for c, f in enumerate(faces))
            if score > best_score:
                best, best_score = [(f, c) for c, f in enumerate(faces)], score
    else:
        for chars in itertools.permutations(range(n_chars), n_faces):
            score = sum(sim[f, c] for f, c in enumerate(chars))
            if score > best_score:
                best, best_score = list(enumerate(chars)), score
    return best


class IdentityGuard:
    def __init__(self, centroids: dict[str, np.ndarray]):
        self.centroids = {cid: _normalise(np.asarray(c, dtype=np.float32)) for cid, c in centroids.items()}

    def check_clip(
        self,
        frames: list[list[FaceObservation]],
        expected: list[str],
        thresholds: IdentityThresholds,
        faces_required: bool = True,
    ) -> IdentityResult:
        """frames: per sampled frame, the faces detected in it."""
        unknown_ids = [c for c in expected if c not in self.centroids]
        if unknown_ids:
            raise KeyError(f"no locked face centroid for {unknown_ids}; run casting first")

        scores = {cid: CharacterScore(cid) for cid in expected}
        matrix = np.stack([self.centroids[c] for c in expected]) if expected else np.zeros((0, 0))
        unknown_faces = 0

        for faces in frames:
            faces = [f for f in faces if f.size_px >= thresholds.min_face_px]
            matched: set[str] = set()
            if faces and expected:
                emb = np.stack([_normalise(f.embedding) for f in faces])
                sim = emb @ matrix.T
                for f_idx, c_idx in assign_faces(sim):
                    cid = expected[c_idx]
                    # Swap/bleed: this face looks more like another expected character.
                    if len(expected) > 1 and int(np.argmax(sim[f_idx])) != c_idx:
                        scores[cid].swapped_frames += 1
                    scores[cid].similarities.append(float(sim[f_idx, c_idx]))
                    matched.add(cid)
                unknown_faces += max(0, len(faces) - len(expected))
            elif faces:
                unknown_faces += len(faces)
            for cid in expected:
                if cid not in matched:
                    scores[cid].frames_missing += 1

        issues: list[str] = []
        n = max(1, len(frames))
        for cid, s in scores.items():
            if faces_required and s.frames_missing / n > 0.5:
                issues.append(f"{cid}: face missing in {s.frames_missing}/{len(frames)} sampled frames")
                continue
            if not s.similarities:
                continue
            if s.mean < thresholds.min_mean_similarity:
                issues.append(f"{cid}: mean similarity {s.mean:.3f} < {thresholds.min_mean_similarity}")
            if s.minimum < thresholds.min_frame_similarity:
                issues.append(f"{cid}: min frame similarity {s.minimum:.3f} < {thresholds.min_frame_similarity}")
            if s.std > thresholds.max_similarity_std:
                issues.append(f"{cid}: identity drift, std {s.std:.3f} > {thresholds.max_similarity_std}")
            if s.swapped_frames:
                issues.append(f"{cid}: identity swap/bleed in {s.swapped_frames} frames")
        return IdentityResult(passed=not issues, scores=scores, issues=issues, unknown_faces=unknown_faces)
