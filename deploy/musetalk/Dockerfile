# Lip-sync worker: MuseTalk 1.5 (Tencent Music, MIT code) on one NVIDIA L4, the cheap set's lip-sync engine.
# Same job as deploy/lipsync (LatentSync): a manifest of shots in, synced clips and a status file out, as a Cloud Run
# job, a Vertex AI custom job or a Vertex AI endpoint (python worker.py serve). Built remotely by Cloud Build
# (deploy/musetalk/setup.sh); the pipeline container never pulls it.
# Licence note: the MuseTalk code is MIT (academic and commercial use); the helper models it loads (Whisper tiny,
# sd-vae-ft-mse, DWPose, S3FD, BiSeNet face parsing) each carry their own licence; check them before a commercial
# release, as for LatentSync.
FROM nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 HF_HUB_ENABLE_HF_TRANSFER=0 PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip python3-venv ffmpeg git curl \
        libgl1 libglib2.0-0 libsndfile1 build-essential \
    && rm -rf /var/lib/apt/lists/* && ln -sf /usr/bin/python3 /usr/local/bin/python
RUN python -m pip install --upgrade "pip<25" setuptools wheel

# The recipe MuseTalk's README gives (Python 3.10, PyTorch 2.0.1 on CUDA 11.8, then its requirements and the MMLab
# packages through openmim), pinned to one commit so a rebuild is the same image.
ARG MUSETALK_REF=0a89dec45a0192b824e3cf4daf96c239440c5ed8
RUN git clone https://github.com/TMElyralab/MuseTalk /opt/MuseTalk \
    && cd /opt/MuseTalk && git checkout ${MUSETALK_REF}
WORKDIR /opt/MuseTalk
RUN pip install torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2 --index-url https://download.pytorch.org/whl/cu118
# gradio is the demo web page only and tensorflow/tensorboard are for training (nothing under musetalk/ imports
# them); everything else as upstream pins it
RUN grep -v -i -E "^(gradio|tensorflow|tensorboard)" requirements.txt > /tmp/requirements.txt \
    && pip install -r /tmp/requirements.txt \
    && pip install -U openmim && mim install mmengine && mim install "mmcv==2.0.1" && mim install "mmdet==3.1.0" \
    && mim install "mmpose==1.1.0" && pip install google-cloud-storage "huggingface_hub[cli]==0.30.2"

# Weights, laid out as models/ the way download_weights.sh does (MuseTalk 1.5 only; 1.0 and SyncNet are not used).
RUN mkdir -p models/musetalkV15 models/sd-vae models/whisper models/dwpose models/face-parse-bisent \
    && huggingface-cli download TMElyralab/MuseTalk --local-dir models --include "musetalkV15/musetalk.json" "musetalkV15/unet.pth" \
    && huggingface-cli download stabilityai/sd-vae-ft-mse --local-dir models/sd-vae --include "config.json" "diffusion_pytorch_model.bin" \
    && huggingface-cli download openai/whisper-tiny --local-dir models/whisper --include "config.json" "pytorch_model.bin" "preprocessor_config.json" \
    && huggingface-cli download yzd-v/DWPose --local-dir models/dwpose --include "dw-ll_ucoco_384.pth" \
    && curl -fsSL https://download.pytorch.org/models/resnet18-5c106cde.pth -o models/face-parse-bisent/resnet18-5c106cde.pth
# The face-parsing weights come from a Google Drive file (what upstream's script downloads with gdown); when Drive
# refuses the build machine, the same 53,289,463-byte file from a public Hugging Face mirror.
RUN (pip install gdown && gdown 154JgKpzCPW82qINcVieuPH3fZ2e0P812 -O models/face-parse-bisent/79999_iter.pth \
        && test "$(stat -c %s models/face-parse-bisent/79999_iter.pth)" = 53289463) \
    || curl -fsSL https://huggingface.co/camenduru/MuseTalk/resolve/main/face-parse-bisent/79999_iter.pth \
        -o models/face-parse-bisent/79999_iter.pth
# The S3FD face detector the preprocessing downloads on first use, fetched now so the worker never needs the internet
RUN curl -fsSL https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth \
        -o musetalk/utils/face_detection/detection/sfd/s3fd.pth
RUN test "$(stat -c %s models/musetalkV15/unet.pth)" = 3400074924 \
    && test "$(stat -c %s models/face-parse-bisent/79999_iter.pth)" = 53289463 \
    && test "$(stat -c %s musetalk/utils/face_detection/detection/sfd/s3fd.pth)" = 89843225 \
    && test -s models/sd-vae/diffusion_pytorch_model.bin && test -s models/whisper/pytorch_model.bin \
    && test -s models/dwpose/dw-ll_ucoco_384.pth && test -s models/face-parse-bisent/resnet18-5c106cde.pth \
    && echo 1.5 > models/VERSION

# From here on nothing is fetched: every model loads from the image (a worker on a GPU never waits on the internet)
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
COPY worker.py /opt/worker.py
# Prove the image whole before it is pushed: every import, every weight file and the worker's own sync of one second
# of MuseTalk's sample clip, on the build machine's CPU (a few minutes). A broken image never reaches the GPU.
RUN python /opt/worker.py smoke
ENTRYPOINT ["python", "/opt/worker.py"]
