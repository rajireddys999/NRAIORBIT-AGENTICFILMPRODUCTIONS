"""Shared by every test file: no test reaches Cloud Storage by accident."""

import pytest

from nraiorbit.service import progress, queue


@pytest.fixture(autouse=True)
def no_running_record_in_a_real_bucket(monkeypatch):
    """A tick given a bucket name keeps its running record there (service/progress.py). Many tests pass a made-up
    bucket such as "b": on a machine with Google credentials the default tracker would send real requests to it,
    so by default the tracker has no bucket. The progress tests inject their own."""
    monkeypatch.setattr(queue, "TRACKER_FACTORY", lambda bucket, log=print: progress.Tracker(None, log=log))
