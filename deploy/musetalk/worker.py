"""Lip-sync worker: a batch of shots through MuseTalk 1.5 on one GPU. The same contract as deploy/lipsync/worker.py
(LatentSync), so the pipeline's provider drives either image the same way.

    MANIFEST=gs://bucket/lipsync/<run>/manifest.json python worker.py      # as a job (Cloud Run / Vertex custom job)
    python worker.py serve                                                  # as a Vertex AI endpoint: POST the
                                                                            # manifest URI, the batch runs in the
                                                                            # background, the status file says when done
    python worker.py smoke                                                  # at image build: one second of the sample
                                                                            # clip through the whole path, on the CPU
    manifest: {"items": [{"key": "...", "video": "gs://...", "audio": "gs://...", "out": "gs://..."}],
     "fp16": true}

The models are loaded once per batch. Each item's video becomes 25 fps frames and its audio 16 kHz mono (what
MuseTalk was trained on), the mouth is redrawn frame by frame the way MuseTalk's scripts/inference.py does it
(version 1.5: bbox_shift 0, extra margin 10, "jaw" parsing, cheek widths 90), and the result is uploaded to "out" as
soon as that shot is done, so a run cut short keeps every finished shot. A status file <manifest>.done.json lists
every item with ok/error and seconds, so one bad shot never loses the others. The weights are baked into the image;
the job exits 0 and reports per item.
"""

from __future__ import annotations

import copy
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = "/opt/MuseTalk"
V15 = {"bbox_shift": 0, "extra_margin": 10, "parsing_mode": "jaw", "left_cheek_width": 90, "right_cheek_width": 90,
       "audio_padding_length_left": 2, "audio_padding_length_right": 2, "batch_size": 8, "fps": 25}


def split(uri: str) -> tuple[str, str]:
    bucket, _, path = uri.removeprefix("gs://").partition("/")
    return bucket, path


class MuseTalk:
    """MuseTalk 1.5 loaded once (upstream's load_all_model + whisper + face parsing), syncing one clip at a time.
    Everything runs from the repo root, as upstream's scripts do (its model paths are relative)."""

    def __init__(self, fp16: bool = True):
        os.chdir(ROOT)
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import cv2
        import numpy as np
        import torch
        from transformers import WhisperModel

        from musetalk.utils.audio_processor import AudioProcessor
        from musetalk.utils.blending import get_image
        from musetalk.utils.face_parsing import FaceParsing
        from musetalk.utils.preprocessing import coord_placeholder, get_landmark_and_bbox
        from musetalk.utils.utils import datagen, load_all_model

        self.cv2, self.np, self.torch = cv2, np, torch
        self.get_image, self.get_landmark_and_bbox, self.placeholder, self.datagen = \
            get_image, get_landmark_and_bbox, coord_placeholder, datagen
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.fp16 = bool(fp16) and self.device.type == "cuda"      # half precision only makes sense on the GPU
        self.vae, self.unet, self.pe = load_all_model(unet_model_path="models/musetalkV15/unet.pth", vae_type="sd-vae",
                                                      unet_config="models/musetalkV15/musetalk.json", device=self.device)
        self.timesteps = torch.tensor([0], device=self.device)
        if self.fp16:
            self.pe, self.vae.vae, self.unet.model = self.pe.half(), self.vae.vae.half(), self.unet.model.half()
        self.pe, self.vae.vae, self.unet.model = \
            self.pe.to(self.device), self.vae.vae.to(self.device), self.unet.model.to(self.device)
        self.audio = AudioProcessor(feature_extractor_path="models/whisper")
        self.dtype = self.unet.model.dtype
        self.whisper = WhisperModel.from_pretrained("models/whisper").to(device=self.device, dtype=self.dtype).eval()
        self.whisper.requires_grad_(False)
        self.fp = FaceParsing(left_cheek_width=V15["left_cheek_width"], right_cheek_width=V15["right_cheek_width"])

    def sync(self, video: str, audio: str, out: str, work: str) -> None:
        """One shot: `video` (any fps) and `audio` (any rate) to `out` (25 fps H.264 with the audio), via `work`."""
        cv2, np, torch = self.cv2, self.np, self.torch
        frames_dir, a16 = f"{work}/frames", f"{work}/in16.wav"
        os.makedirs(frames_dir, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-r", str(V15["fps"]), "-start_number", "0",
                        f"{frames_dir}/%08d.png"], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio, "-ar", "16000", "-ac", "1", a16], check=True)
        img_list = sorted(glob.glob(f"{frames_dir}/*.png"))
        if not img_list:
            raise RuntimeError("ffmpeg found no frames in the video")
        with torch.no_grad():
            features, length = self.audio.get_audio_feature(a16)
            chunks = self.audio.get_whisper_chunk(features, self.device, self.dtype, self.whisper, length,
                                                  fps=V15["fps"], audio_padding_length_left=V15["audio_padding_length_left"],
                                                  audio_padding_length_right=V15["audio_padding_length_right"])
            coords, frames = self.get_landmark_and_bbox(img_list, V15["bbox_shift"])
            latents = []
            for bbox, frame in zip(coords, frames):
                if bbox == self.placeholder:
                    continue
                x1, y1, x2, y2 = bbox
                y2 = min(y2 + V15["extra_margin"], frame.shape[0])
                crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
                latents.append(self.vae.get_latents_for_unet(crop))
            if not latents:
                raise RuntimeError("MuseTalk found no face in the shot")
            frames_cycle, coords_cycle, latents_cycle = frames + frames[::-1], coords + coords[::-1], latents + latents[::-1]
            gen = self.datagen(whisper_chunks=chunks, vae_encode_latents=latents_cycle, batch_size=V15["batch_size"],
                               delay_frame=0, device=self.device)
            results = []
            for whisper_batch, latent_batch in gen:
                audio_features = self.pe(whisper_batch)
                latent_batch = latent_batch.to(dtype=self.dtype)
                pred = self.unet.model(latent_batch, self.timesteps, encoder_hidden_states=audio_features).sample
                results.extend(self.vae.decode_latents(pred))
        out_dir = f"{work}/out_frames"
        os.makedirs(out_dir, exist_ok=True)
        for i, res in enumerate(results):
            bbox = coords_cycle[i % len(coords_cycle)]
            frame = copy.deepcopy(frames_cycle[i % len(frames_cycle)])
            x1, y1, x2, y2 = bbox
            y2 = min(y2 + V15["extra_margin"], frame.shape[0])
            try:
                res = cv2.resize(res.astype(np.uint8), (x2 - x1, y2 - y1))
            except Exception:
                continue
            combined = self.get_image(frame, res, [x1, y1, x2, y2], mode=V15["parsing_mode"], fp=self.fp)
            cv2.imwrite(f"{out_dir}/{i:08d}.png", combined)
        silent = f"{work}/silent.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-r", str(V15["fps"]), "-f", "image2", "-i", f"{out_dir}/%08d.png",
                        "-vcodec", "libx264", "-vf", "format=yuv420p", "-crf", "18", silent], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", silent, "-i", audio, "-c:v", "copy", "-c:a", "aac",
                        "-shortest", out], check=True)
        shutil.rmtree(frames_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


def main(manifest_uri: str | None = None) -> int:
    from google.cloud import storage

    client = storage.Client()
    manifest_uri = manifest_uri or os.environ["MANIFEST"]
    b, p = split(manifest_uri)
    manifest = json.loads(client.bucket(b).blob(p).download_as_text())
    version = open(f"{ROOT}/models/VERSION").read().strip() if os.path.exists(f"{ROOT}/models/VERSION") else "1.5"
    loaded = time.time()
    model = MuseTalk(fp16=manifest.get("fp16", True))
    print(json.dumps({"loaded": "MuseTalk " + version, "seconds": round(time.time() - loaded, 1),
                      "device": str(model.device), "fp16": model.fp16}), flush=True)
    results = []
    for item in manifest["items"]:
        started = time.time()
        try:
            with tempfile.TemporaryDirectory() as d:
                video, audio, out = f"{d}/in.mp4", f"{d}/in.wav", f"{d}/out.mp4"
                for uri, path in ((item["video"], video), (item["audio"], audio)):
                    vb, vp = split(uri)
                    client.bucket(vb).blob(vp).download_to_filename(path)
                model.sync(video, audio, out, d)
                ob, op = split(item["out"])
                client.bucket(ob).blob(op).upload_from_filename(out, content_type="video/mp4")
            results.append({"key": item["key"], "ok": True, "seconds": round(time.time() - started, 1)})
        except Exception:                               # report and carry on with the next shot
            results.append({"key": item["key"], "ok": False, "seconds": round(time.time() - started, 1),
                            "error": traceback.format_exc()[-3000:]})
        print(json.dumps(results[-1])[:500], flush=True)
    status = {"version": version, "engine": "musetalk", "fp16": model.fp16, "results": results}
    client.bucket(b).blob(p.removesuffix(".json") + ".done.json").upload_from_string(
        json.dumps(status, indent=1), content_type="application/json")
    return 0


def smoke() -> int:
    """At image build: the first second of MuseTalk's own sample clip through the worker's path on the CPU. Fails
    the build when an import, a weight file or the sync itself is broken."""
    with tempfile.TemporaryDirectory() as d:
        video, audio, out = f"{d}/in.mp4", f"{d}/in.wav", f"{d}/out.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"{ROOT}/data/video/yongen.mp4", "-t", "1", "-an",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", video], check=True)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", f"{ROOT}/data/audio/yongen.wav", "-t", "1", audio], check=True)
        started = time.time()
        MuseTalk(fp16=False).sync(video, audio, out, d)
        size = os.path.getsize(out)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=nb_read_frames,width,height,r_frame_rate", "-of", "json", out],
                               capture_output=True, text=True, check=True).stdout
        print(json.dumps({"smoke": "ok", "seconds": round(time.time() - started, 1), "bytes": size,
                          "video": json.loads(probe)["streams"][0]}), flush=True)
        if size < 10_000:
            raise SystemExit("smoke: the synced clip is implausibly small")
    return 0


def serve() -> None:
    """Vertex AI custom-container serving: health on AIP_HEALTH_ROUTE, batches on AIP_PREDICT_ROUTE. A predict call
    queues {"instances": [{"manifest": "gs://..."}]} and returns at once (batches take minutes, longer than a
    request may last); the caller watches for the manifest's .done.json."""
    import queue
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    work: queue.Queue[str] = queue.Queue()

    def runner() -> None:
        while True:
            uri = work.get()
            try:
                main(uri)
            except Exception:
                traceback.print_exc()

    threading.Thread(target=runner, daemon=True).start()
    health, predict = os.environ.get("AIP_HEALTH_ROUTE", "/health"), os.environ.get("AIP_PREDICT_ROUTE", "/predict")

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._reply(200 if self.path.startswith(health) else 404, {"ok": True})

        def do_POST(self):  # noqa: N802
            if not self.path.startswith(predict):
                return self._reply(404, {"error": "not found"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            uris = [i["manifest"] for i in body.get("instances", []) if "manifest" in i]
            for uri in uris:
                work.put(uri)
            self._reply(200, {"predictions": [{"queued": u} for u in uris]})

    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("AIP_HTTP_PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    if sys.argv[1:] == ["smoke"]:
        sys.exit(smoke())
    sys.exit(main())
