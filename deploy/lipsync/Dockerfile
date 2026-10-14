# Lip-sync worker: LatentSync (ByteDance, Apache-2.0 code) on one NVIDIA L4, run as a Cloud Run job.
# Built remotely by Cloud Build (deploy/lipsync/setup.sh); the pipeline container never pulls it.
# Licence note: check the weights' licence and the face detector LatentSync uses before a commercial release.
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 HF_HUB_ENABLE_HF_TRANSFER=0
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ARG LATENTSYNC_REF=main
RUN git clone https://github.com/bytedance/LatentSync /opt/LatentSync \
    && cd /opt/LatentSync && git checkout ${LATENTSYNC_REF}
WORKDIR /opt/LatentSync
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir "huggingface_hub>=0.24" google-cloud-storage

# Weights: LatentSync 1.6 (512 px face) if it can be downloaded, else 1.5 (256 px); the worker reads the marker.
RUN mkdir -p checkpoints/whisper \
    && (huggingface-cli download ByteDance/LatentSync-1.6 latentsync_unet.pt whisper/tiny.pt --local-dir checkpoints \
        && echo 1.6 > checkpoints/VERSION) \
    || (huggingface-cli download ByteDance/LatentSync-1.5 latentsync_unet.pt whisper/tiny.pt --local-dir checkpoints \
        && echo 1.5 > checkpoints/VERSION)
RUN test -s checkpoints/latentsync_unet.pt && test -s checkpoints/whisper/tiny.pt && cat checkpoints/VERSION

# Pre-fetch any helper models the inference code downloads on first use (best effort, CPU only).
RUN python -c "import latentsync.utils.util" 2>/dev/null || true

COPY worker.py /opt/worker.py
ENTRYPOINT ["python", "/opt/worker.py"]
