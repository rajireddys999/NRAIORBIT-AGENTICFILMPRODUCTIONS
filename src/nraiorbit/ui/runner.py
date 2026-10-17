"""The runner as the control room sees it: the Cloud Run job's recent executions, one execution's log lines, and
the button that starts a tick now. Every call fails soft (a message instead of a crash) so the page still renders
without the cloud roles or off the cloud."""

from __future__ import annotations

import datetime


def _creds():
    try:
        from ..providers.cloud_lipsync import _credentials
        return _credentials()
    except Exception:
        return None


def job_name(project: str, region: str, job: str) -> str:
    return f"projects/{project}/locations/{region}/jobs/{job}"


def executions(project: str | None, region: str, job: str | None, limit: int = 6) -> list[dict]:
    if not project or not job:
        return []
    from google.cloud import run_v2

    client = run_v2.ExecutionsClient(credentials=_creds())
    out = []
    for ex in client.list_executions(parent=job_name(project, region, job)):
        out.append({
            "name": ex.name.rsplit("/", 1)[-1],
            "created": ex.create_time.isoformat(timespec="seconds") if ex.create_time else "",
            "completed": ex.completion_time.isoformat(timespec="seconds") if ex.completion_time else "",
            "succeeded": ex.succeeded_count, "failed": ex.failed_count, "running": ex.running_count,
            "state": ("running" if ex.running_count else "failed" if ex.failed_count else "ok" if ex.succeeded_count
                      else "pending"),
        })
        if len(out) >= limit:
            break
    return out


def run(project: str, region: str, job: str) -> str:
    """Start one tick now. Returns the execution's short name."""
    from google.cloud import run_v2

    client = run_v2.JobsClient(credentials=_creds())
    op = client.run_job(request={"name": job_name(project, region, job)})
    try:
        return op.metadata.name.rsplit("/", 1)[-1]
    except Exception:
        return "started"


def logs(project: str | None, execution: str | None, limit: int = 80) -> list[str]:
    if not project or not execution:
        return []
    from google.cloud import logging as cloud_logging

    client = cloud_logging.Client(project=project, credentials=_creds())
    flt = (f'resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="{execution}"')
    lines = []
    for entry in client.list_entries(filter_=flt, order_by=cloud_logging.DESCENDING, max_results=limit):
        payload = entry.payload if isinstance(entry.payload, str) else str(entry.payload)
        ts = entry.timestamp.astimezone(datetime.timezone.utc).strftime("%H:%M:%S") if entry.timestamp else ""
        lines.append(f"{ts}  {payload}".rstrip())
    return list(reversed(lines))
