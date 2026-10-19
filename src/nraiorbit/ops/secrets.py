"""Secret Manager, the little we need: read the latest version of a secret, add a version. Signs in like the rest of
the framework (the service account key in GOOGLE_APPLICATION_CREDENTIALS_JSON, else the environment's identity).
Every call fails soft to None/False so a laptop without the API still works from files and environment variables."""

from __future__ import annotations

import os


def _client():
    from google.cloud import secretmanager

    from ..providers.cloud_lipsync import _credentials
    return secretmanager.SecretManagerServiceClient(credentials=_credentials())


def project() -> str | None:
    return os.environ.get("GOOGLE_CLOUD_PROJECT")


def read(name: str, project_id: str | None = None) -> str | None:
    """The latest version's text, or None (no project, no such secret, no version, no permission)."""
    pid = project_id or project()
    if not pid:
        return None
    try:
        out = _client().access_secret_version(request={"name": f"projects/{pid}/secrets/{name}/versions/latest"})
        return out.payload.data.decode("utf-8")
    except Exception:
        return None


def add_version(name: str, payload: str, project_id: str | None = None) -> bool:
    pid = project_id or project()
    if not pid:
        return False
    try:
        _client().add_secret_version(request={"parent": f"projects/{pid}/secrets/{name}",
                                              "payload": {"data": payload.encode("utf-8")}})
        return True
    except Exception:
        return False
