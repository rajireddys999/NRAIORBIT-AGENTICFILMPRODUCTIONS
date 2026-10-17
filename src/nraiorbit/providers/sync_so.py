"""Lip sync with sync.so (sync-3 or lipsync-2-pro) over its REST API: sharp 4K-native mouths, no GPU of our own.

    providers.lipsync: sync_so
    lipsync: {model: sync-3, bucket: <project>-media, sync_mode: cut_off, concurrency: 1}
    environment: SYNC_API_KEY (the key never goes in a file; see lipsync.api_key_env to use another variable)

Every shot's clip and its dialogue audio are staged in the private bucket under lipsync/syncso/<run>/ and handed to
sync.so as time-limited signed links. The finished clip is downloaded, conformed to the input's frame size, frame
rate and frame count (so the paste-back, speaker lock and per-shot settings work exactly as with the GPU worker) and
cached in cache/lipsync/ by content, so a re-render never pays twice. Each job's cost (seconds x the model's price per
second) is checked against the budget before it is submitted and logged to reports/cost.jsonl.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ..costing import SpendLedger
from ..post import ffmpeg
from .base import register
from .cloud_lipsync import _credentials

STORAGE_FACTORY = None          # tests inject fakes here
HTTP_FACTORY = None
SLEEP = time.sleep

DONE = {"COMPLETED"}
DEAD = {"FAILED", "REJECTED", "CANCELED", "CANCELLED", "TIMED_OUT", "ERROR"}
DEFAULT_PER_SECOND = {"sync-3": 0.133, "lipsync-2-pro": 0.083, "lipsync-2": 0.04}
USER_AGENT = "nraiorbit-lipsync/1.0 (python-requests)"      # the bare urllib identity is refused at the edge


class SyncSoError(RuntimeError):
    pass


def probe_video(path: Path) -> tuple[int, int, float]:
    """(width, height, fps) of the first video stream."""
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height,r_frame_rate", "-of", "json", str(path)],
                          capture_output=True, text=True, check=True)
    s = json.loads(proc.stdout)["streams"][0]
    num, _, den = str(s.get("r_frame_rate", "24/1")).partition("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den or 1)


def conform_like(src: Path, like: Path, out: Path) -> Path:
    """The downloaded result cut to the same frame size, frame rate and frame count as the clip it was made from, so
    frame i of the result is frame i of the shot (a short result holds its last frame, a long one is cut)."""
    w, h, fps = probe_video(like)
    n = ffmpeg.frame_count(like)
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(["-i", str(src), "-an",
                "-vf", f"scale={w}:{h}:flags=lanczos,setsar=1,fps={fps:g},tpad=stop_mode=clone:stop_duration=10",
                "-frames:v", str(n), *ffmpeg.VIDEO_ENCODE, str(out)])
    return out


@register("lipsync", "sync_so")
class SyncSoLipSync:
    def __init__(self, project_root: Path, cfg: dict, pricing: dict, ledger: SpendLedger,
                 storage_client=None, http=None):
        ls = cfg.get("lipsync") or {}
        self.model = ls.get("model", "sync-3")
        self.sync_mode = ls.get("sync_mode", "cut_off")
        self.project = ls.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.bucket = ls.get("bucket") or (f"{self.project}-media" if self.project else None)
        self.api_base = ls.get("api_base", "https://api.sync.so").rstrip("/")
        self.api_key_env = ls.get("api_key_env", "SYNC_API_KEY")
        self.poll_s = float(ls.get("poll_seconds", 10))
        self.timeout_s = float(ls.get("timeout_seconds", 1800))
        self.concurrency = max(1, int(ls.get("concurrency", 1)))
        self.url_ttl = datetime.timedelta(minutes=int(ls.get("url_ttl_minutes", 180)))
        self.on_failure = ls.get("on_failure", "stop")          # stop | keep_original (as the GPU worker)
        self.retry_failed = bool(ls.get("retry_failed", False))
        rates = (pricing.get("sync_so") or {}).get("per_second") or {}
        self.per_second = float(rates.get(self.model, DEFAULT_PER_SECOND.get(self.model, 0.133)))
        self.cache = project_root / "cache" / "lipsync"
        self.ledger = ledger
        self._storage, self._http = storage_client, http

    # -- clients (made on first use, so cached results need no sign-in or key)

    def storage(self):
        if self._storage is None:
            if STORAGE_FACTORY is not None:
                self._storage = STORAGE_FACTORY()
            else:
                from google.cloud import storage
                self._storage = storage.Client(project=self.project, credentials=_credentials())
        return self._storage

    def http(self):
        if self._http is None:
            if HTTP_FACTORY is not None:
                self._http = HTTP_FACTORY()
            else:
                import requests
                key = os.environ.get(self.api_key_env, "").strip()
                if not key:
                    raise SyncSoError(f"lip-sync: the sync.so API key is missing; set {self.api_key_env} in the "
                                      "environment (never in a file or the chat).")
                s = requests.Session()
                s.headers.update({"x-api-key": key, "User-Agent": USER_AGENT, "Accept": "application/json"})
                self._http = s
        return self._http

    # -- provider API (the same as the GPU worker's)

    def key(self, video: Path, audio: Path) -> str:
        h = hashlib.sha1(json.dumps(["sync_so", self.model, self.sync_mode]).encode())
        h.update(video.read_bytes())
        h.update(audio.read_bytes())
        return h.hexdigest()[:16]

    def apply(self, video: Path, audio: Path, out: Path) -> Path | None:
        return self.apply_many([(video, audio, out)])[0]

    def cached(self, video: Path, audio: Path) -> Path | None:
        path = self.cache / f"{self.key(video, audio)}.mp4"
        return path if path.exists() else None

    def failure(self, video: Path, audio: Path) -> str | None:
        marker = self.cache / f"{self.key(video, audio)}.failed.json"
        return json.loads(marker.read_text(encoding="utf-8")).get("error") if marker.exists() else None

    def apply_many(self, items: list[tuple[Path, Path, Path]]) -> list[Path | None]:
        keys = [self.key(v, a) for v, a, _ in items]
        known_bad = set() if self.retry_failed else {k for k in keys if (self.cache / f"{k}.failed.json").exists()}
        todo = {k: (v, a) for k, (v, a, _) in zip(keys, items)
                if not (self.cache / f"{k}.mp4").exists() and k not in known_bad}
        if todo:
            self._run(todo)
        outs = []
        for k, (_, _, out) in zip(keys, items):
            if not (self.cache / f"{k}.mp4").exists():
                outs.append(None)
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.cache / f"{k}.mp4", out)
            outs.append(out)
        return outs

    # -- the jobs: stage, submit (up to `concurrency` at once), poll, download, conform, cache

    def _run(self, todo: dict[str, tuple[Path, Path]]) -> None:
        if not self.bucket:
            raise SyncSoError("lip-sync: set lipsync.bucket (or GOOGLE_CLOUD_PROJECT) to stage clips for sync.so.")
        run = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.cache.mkdir(parents=True, exist_ok=True)
        queue = list(todo.items())
        active: dict[str, dict] = {}                         # key -> {id, video, audio, seconds, started}
        while queue or active:
            while queue and len(active) < self.concurrency:
                key, (video, audio) = queue.pop(0)
                active[key] = self._submit(run, key, video, audio)
            SLEEP(self.poll_s)
            for key in list(active):
                job = active[key]
                data = self._get(f"{self.api_base}/v2/generate/{job['id']}", f"sync.so status {key}")
                status = str(data.get("status", "")).upper()
                if status in DONE:
                    self._finish(key, job, data)
                    del active[key]
                elif status in DEAD:
                    self._failed(key, job, data.get("error") or data.get("message") or status)
                    del active[key]
                elif time.monotonic() - job["started"] > self.timeout_s:
                    self._failed(key, job, f"no result after {self.timeout_s:.0f} s")
                    del active[key]

    def _submit(self, run: str, key: str, video: Path, audio: Path) -> dict:
        seconds = ffmpeg.duration(video)
        usd = seconds * self.per_second
        self.ledger.check(usd, f"sync.so {self.model} {key}")
        bucket = self.storage().bucket(self.bucket)
        urls = []
        for kind, path, ctype in (("video", video, "video/mp4"), ("audio", audio, "audio/wav")):
            blob = bucket.blob(f"lipsync/syncso/{run}/{key}/{path.name}")
            blob.upload_from_filename(str(path), content_type=ctype)
            urls.append({"type": kind, "url": blob.generate_signed_url(version="v4", expiration=self.url_ttl,
                                                                      method="GET")})
        body = {"model": self.model, "input": urls, "options": {"sync_mode": self.sync_mode},
                "outputFileName": f"{key}"}
        data = self._post(f"{self.api_base}/v2/generate", body, f"sync.so generate {key}")
        if not data.get("id"):
            raise SyncSoError(f"lip-sync: sync.so answered without a generation id: {json.dumps(data)[:300]}")
        return {"id": data["id"], "video": video, "audio": audio, "seconds": seconds, "usd": usd,
                "started": time.monotonic(), "run": run}

    def _finish(self, key: str, job: dict, data: dict) -> None:
        url = _output_url(data)
        if not url:
            self._failed(key, job, f"completed without an output url: {json.dumps(data)[:300]}")
            return
        raw = self.cache / f"{key}.download.mp4"
        self._download(url, raw)
        conform_like(raw, job["video"], self.cache / f"{key}.mp4")
        raw.unlink(missing_ok=True)
        self.ledger.record("sync_so", f"{self.model} {key}", job["usd"], seconds=round(job["seconds"], 2),
                           model=self.model, generation=job["id"], run=job["run"])

    def _failed(self, key: str, job: dict, error: str) -> None:
        if self.on_failure != "keep_original":
            raise SyncSoError(f"lip-sync: sync.so could not do {job['video'].name}: {error}")
        (self.cache / f"{key}.failed.json").write_text(json.dumps({"error": str(error), "generation": job["id"],
                                                                   "model": self.model}), encoding="utf-8")

    # -- HTTP with short retries on 429/5xx; a proxy denial names the host so the owner can allow it

    def _post(self, url: str, body: dict, what: str) -> dict:
        return self._call("post", url, what, json=body)

    def _get(self, url: str, what: str) -> dict:
        return self._call("get", url, what)

    def _call(self, method: str, url: str, what: str, **kw) -> dict:
        last = None
        for attempt in range(5):
            try:
                resp = getattr(self.http(), method)(url, timeout=120, **kw)
            except Exception as exc:                       # connection refused by the proxy, DNS, timeout
                last = f"{type(exc).__name__}: {exc}"
                SLEEP(2 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {resp.status_code}: {getattr(resp, 'text', '')[:200]}"
                SLEEP(3 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise SyncSoError(f"{what}: HTTP {resp.status_code}: {getattr(resp, 'text', '')[:300]}")
            return resp.json()
        raise SyncSoError(f"{what}: gave up after retries ({last})")

    def _download(self, url: str, out: Path) -> None:
        import requests
        try:
            with requests.get(url, stream=True, timeout=300, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status_code >= 400:
                    raise SyncSoError(f"lip-sync: download of the sync.so result failed: HTTP {resp.status_code} "
                                      f"from {url.split('/')[2]}")
                with open(out, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
        except requests.RequestException as exc:
            raise SyncSoError(f"lip-sync: could not download the sync.so result from {url.split('/')[2]} "
                              f"({type(exc).__name__}); if this is a network policy denial, allow that host.") from exc


def _output_url(data: dict) -> str | None:
    for k in ("outputUrl", "output_url", "url"):
        if data.get(k):
            return data[k]
    for k in ("outputs", "output"):
        v = data.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("url"):
            return v[0]["url"]
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    return None
