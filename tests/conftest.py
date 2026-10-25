"""Shared by every test file: no test reaches Cloud Storage by accident."""

import pytest

from nraiorbit.service import progress, queue


@pytest.fixture(autouse=True)
def no_running_record_in_a_real_bucket(monkeypatch):
    """A tick given a bucket name keeps its running record there (service/progress.py). Many tests pass a made-up
    bucket such as "b": on a machine with Google credentials the default tracker would send real requests to it,
    so by default the tracker has no bucket. The progress tests inject their own."""
    monkeypatch.setattr(queue, "TRACKER_FACTORY", lambda bucket, log=print: progress.Tracker(None, log=log))


@pytest.fixture(autouse=True)
def no_shorts_requests_in_a_real_bucket(monkeypatch):
    """After its story step a tick with a bucket looks for the owner's Shorts requests there (service/shorts.py); the
    tests that drive that step put their own in its place."""
    monkeypatch.setattr(queue, "SHORTS", lambda bucket, rows, log: [])


@pytest.fixture(autouse=True)
def no_cloud_run_lookup_for_the_footer(monkeypatch):
    """Every control-room page shows the runner's live image (ui/runner.job_image, read from Cloud Run). Off the
    cloud that lookup waits seconds for credentials, so the tests see a fixed image; the footer test sets its own."""
    from nraiorbit.ui import runner as runner_mod
    monkeypatch.setattr(runner_mod, "job_image", lambda project, region, job: "")


@pytest.fixture(autouse=True)
def no_google_credentials(monkeypatch):
    """No test uses real Google credentials. Without this, every storage client a test makes asks for them, and the
    library probes the cloud metadata server with retries first: about 4.5 s each time on a laptop, longer on CI."""
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError:
        return

    def refuse(*args, **kwargs):
        raise DefaultCredentialsError("tests never use Google credentials")
    monkeypatch.setattr(google.auth, "default", refuse)


@pytest.fixture(autouse=True)
def no_gcloud(monkeypatch):
    """With no storage client the code falls back to the gcloud command, which on a machine logged in to gcloud would
    reach a real bucket (many tests pass a made-up one such as "b") and costs seconds on CI. Here it fails at once,
    as gcloud with no account does; the tests that drive gcloud put their own recorder in its place."""
    import subprocess

    from nraiorbit.release import gcs

    def refuse(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="ERROR: (gcloud) You do not currently have an "
                                                                     "active account selected. (tests never run gcloud)")
    monkeypatch.setattr(gcs, "RUN", refuse)


@pytest.fixture(autouse=True)
def fastest_encoder(monkeypatch):
    """The end-to-end tests cut whole films at 1080p; x264's fastest preset makes the same frames at the same quality
    (crf) in a fraction of the time, larger files aside. Real films keep ffmpeg.VIDEO_ENCODE as it is."""
    from nraiorbit.post import ffmpeg
    fast = list(ffmpeg.VIDEO_ENCODE)
    fast[fast.index("-preset") + 1] = "ultrafast"
    monkeypatch.setattr(ffmpeg, "VIDEO_ENCODE", fast)
