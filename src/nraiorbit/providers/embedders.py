"""Optional analysis adapters for the guards (heavy deps, imported lazily).

Not exercised in CI. Check licences before commercial use (docs/TOOLS.md §9):
InsightFace's pretrained face models are licensed for non-commercial research only.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from ..guards.identity import FaceObservation
from .base import register


@register("face_embedder", "insightface")
class InsightFaceEmbedder:
    """RetinaFace detection + ArcFace embeddings via `insightface` (pip install insightface onnxruntime-gpu opencv-python)."""

    def __init__(self, model_pack: str = "buffalo_l", ctx_id: int = 0, det_size: tuple[int, int] = (640, 640)):
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(name=model_pack, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.app.prepare(ctx_id=ctx_id, det_size=det_size)

    def detect_and_embed(self, image_path: Path) -> list[FaceObservation]:
        import cv2

        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(image_path)
        return [
            FaceObservation(embedding=np.asarray(f.normed_embedding, dtype=np.float32),
                            bbox=tuple(float(v) for v in f.bbox), det_score=float(f.det_score))
            for f in self.app.get(img)
        ]


@register("speaker_embedder", "speechbrain_ecapa")
class SpeechBrainSpeakerEmbedder:
    """ECAPA-TDNN speaker embeddings (pip install speechbrain torchaudio)."""

    def __init__(self, source: str = "speechbrain/spkrec-ecapa-voxceleb", device: str = "cuda"):
        from speechbrain.inference.speaker import EncoderClassifier

        self.model = EncoderClassifier.from_hparams(source=source, run_opts={"device": device})

    def embed(self, wav_path: Path) -> np.ndarray:
        import torchaudio

        signal, sr = torchaudio.load(str(wav_path))
        if sr != 16000:
            signal = torchaudio.functional.resample(signal, sr, 16000)
        signal = signal.mean(dim=0, keepdim=True)
        return self.model.encode_batch(signal).squeeze().detach().cpu().numpy()
