"""Where the control room reads and writes: the media bucket (gs://bucket) or a local folder shaped like it.

Both hold the same tree: <slug>/project/<plan files>, <slug>/<stage folders>, queue/*.csv, stories/*, library/*.
A local root is the projects/ folder on a laptop (the runner's layout is the backup of a project folder, plus
`project/` for the plan files; locally those sit at the top, so `project/<file>` falls back to `<file>`).
"""

from __future__ import annotations

import datetime
import mimetypes
from pathlib import Path


class Store:
    def __init__(self, root: str, link_minutes: int = 120):
        self.root = str(root).rstrip("/")
        self.is_gs = self.root.startswith("gs://")
        self.link_minutes = link_minutes
        if self.is_gs:
            rest = self.root[5:]
            self.bucket_name, _, self.prefix = rest.partition("/")
            self._bucket = None
        else:
            self.path = Path(self.root)

    # ------------------------------------------------------------------ gs
    @property
    def bucket(self):
        if self._bucket is None:
            from ..release import gcs
            self._bucket = gcs.client().bucket(self.bucket_name)
        return self._bucket

    def _key(self, rel: str) -> str:
        return f"{self.prefix}/{rel}" if self.prefix else rel

    def _local(self, rel: str) -> Path:
        p = self.path / rel
        if not p.exists() and rel.split("/", 2)[1:2] == ["project"]:      # <slug>/project/x -> <slug>/x locally
            slug, _, tail = rel.split("/", 2)
            alt = self.path / slug / tail
            if alt.exists():
                return alt
        return p

    # ------------------------------------------------------------------ reads
    def exists(self, rel: str) -> bool:
        if self.is_gs:
            return self.bucket.blob(self._key(rel)).exists()
        return self._local(rel).exists()

    def read_text(self, rel: str, default: str | None = None) -> str | None:
        if self.is_gs:
            blob = self.bucket.blob(self._key(rel))
            try:
                return blob.download_as_text()
            except Exception:
                return default
        p = self._local(rel)
        return p.read_text(encoding="utf-8") if p.is_file() else default

    def read_bytes(self, rel: str) -> bytes | None:
        if self.is_gs:
            try:
                return self.bucket.blob(self._key(rel)).download_as_bytes()
            except Exception:
                return None
        p = self._local(rel)
        return p.read_bytes() if p.is_file() else None

    def list(self, prefix: str) -> list[str]:
        """Every object under `prefix` (a folder path ending in /), as paths relative to the root, sorted."""
        if self.is_gs:
            key = self._key(prefix)
            cut = len(self.prefix) + 1 if self.prefix else 0
            return sorted(b.name[cut:] for b in self.bucket.list_blobs(prefix=key) if not b.name.endswith("/"))
        base = self._local(prefix.rstrip("/")) if prefix else self.path
        if not base.is_dir():
            return []
        rel_base = prefix.rstrip("/")
        return sorted(f"{rel_base}/{p.relative_to(base).as_posix()}" if rel_base else p.relative_to(base).as_posix()
                      for p in base.rglob("*") if p.is_file())

    def dirs(self, prefix: str = "") -> list[str]:
        """Immediate sub-folders of `prefix`, as names."""
        if self.is_gs:
            key = self._key(prefix) if prefix else (self.prefix + "/" if self.prefix else "")
            it = self.bucket.list_blobs(prefix=key, delimiter="/")
            list(it)                                            # the prefixes come with the pages
            return sorted(p[len(key):].rstrip("/") for p in it.prefixes)
        base = self._local(prefix.rstrip("/")) if prefix else self.path
        return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []

    # ------------------------------------------------------------------ writes (queue sheets and stories only)
    def write_text(self, rel: str, text: str, content_type: str | None = None) -> None:
        if self.is_gs:
            ct = content_type or mimetypes.guess_type(rel)[0] or "text/plain"
            self.bucket.blob(self._key(rel)).upload_from_string(text, content_type=ct)
            return
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def uri(self, rel: str) -> str:
        """The address the runner understands: gs://bucket/<rel> or an absolute local path."""
        return f"{self.root}/{rel}" if self.is_gs else str((self.path / rel).resolve())

    # ------------------------------------------------------------------ links
    def url(self, rel: str, filename: str | None = None) -> str:
        """A link the browser can open: a time-limited signed link on the bucket, the app's file route locally."""
        if self.is_gs:
            from ..release import gcs
            blob = self.bucket.blob(self._key(rel))
            kw = {"response_disposition": f'attachment; filename="{filename}"'} if filename else {}
            try:
                return blob.generate_signed_url(version="v4", expiration=datetime.timedelta(minutes=self.link_minutes),
                                                method="GET", **kw)
            except Exception:
                return gcs.signed_url(self.bucket_name, self._key(rel), days=max(1, self.link_minutes // 1440),
                                      filename=filename)
        return "/file/" + rel
