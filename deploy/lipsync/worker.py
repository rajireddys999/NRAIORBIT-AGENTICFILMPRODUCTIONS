"""Lip-sync worker: a batch of shots through LatentSync on one GPU.

    MANIFEST=gs://bucket/lipsync/<run>/manifest.json python worker.py      # as a job (Cloud Run / Vertex custom job)
    python worker.py serve                                                  # as a Vertex AI endpoint: POST the
                                                                            # manifest URI, the batch runs in the
                                                                            # background, the status file says when done
    manifest: {"items": [{"key": "...", "video": "gs://...", "audio": "gs://...", "out": "gs://..."}],
     "steps": 20, "guidance": 1.5}

Each item's video is converted to 25 fps and its audio to 16 kHz mono (what LatentSync expects), lip-synced, and the
result uploaded to "out". A status file <manifest>.done.json lists every item with ok/error and seconds, so one bad
shot never loses the others. The model weights are baked into the image; the job exits 0 and reports per item.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

from google.cloud import storage

ROOT = "/opt/LatentSync"
CONFIGS = {"1.6": "configs/unet/stage2_512.yaml", "1.5": "configs/unet/stage2.yaml"}


def split(uri: str) -> tuple[str, str]:
    bucket, _, path = uri.removeprefix("gs://").partition("/")
    return bucket, path


def main(manifest_uri: str | None = None) -> int:
    client = storage.Client()
    manifest_uri = manifest_uri or os.environ["MANIFEST"]
    b, p = split(manifest_uri)
    manifest = json.loads(client.bucket(b).blob(p).download_as_text())
    version = open(f"{ROOT}/checkpoints/VERSION").read().strip() if os.path.exists(f"{ROOT}/checkpoints/VERSION") else "1.5"
    config = CONFIGS.get(version, CONFIGS["1.5"])
    steps, guidance = str(manifest.get("steps", 20)), str(manifest.get("guidance", 1.5))
    results = []
    for item in manifest["items"]:
        started = time.time()
        try:
            with tempfile.TemporaryDirectory() as d:
                video, audio, out = f"{d}/in.mp4", f"{d}/in.wav", f"{d}/out.mp4"
                for uri, path in ((item["video"], video), (item["audio"], audio)):
                    vb, vp = split(uri)
                    client.bucket(vb).blob(vp).download_to_filename(path)
                v25, a16 = f"{d}/in25.mp4", f"{d}/in16.wav"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-r", "25", "-an", "-c:v", "libx264",
                                "-crf", "14", "-pix_fmt", "yuv420p", v25], check=True)
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio, "-ar", "16000", "-ac", "1", a16], check=True)
                subprocess.run([sys.executable, "-m", "scripts.inference", "--unet_config_path", config,
                                "--inference_ckpt_path", "checkpoints/latentsync_unet.pt", "--inference_steps", steps,
                                "--guidance_scale", guidance, "--video_path", v25, "--audio_path", a16,
                                "--video_out_path", out], cwd=ROOT, check=True)
                ob, op = split(item["out"])
                client.bucket(ob).blob(op).upload_from_filename(out, content_type="video/mp4")
            results.append({"key": item["key"], "ok": True, "seconds": round(time.time() - started, 1)})
        except Exception:                               # report and carry on with the next shot
            results.append({"key": item["key"], "ok": False, "seconds": round(time.time() - started, 1),
                            "error": traceback.format_exc()[-3000:]})
        print(json.dumps(results[-1])[:500], flush=True)
    status = {"version": version, "config": config, "results": results}
    client.bucket(b).blob(p.removesuffix(".json") + ".done.json").upload_from_string(
        json.dumps(status, indent=1), content_type="application/json")
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
    sys.exit(main())
