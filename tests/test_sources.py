"""Live smoke tests: each collector hits the real network once."""

import httpx
import pytest

from marine_jobs.models import Job
from marine_jobs.sources import ats, feeds


@pytest.mark.parametrize("module", [feeds, ats])
def test_live_fetch(module):
    with httpx.Client(follow_redirects=True) as client:
        try:
            jobs = module.fetch(client)
        except httpx.TransportError as exc:
            pytest.skip(f"network error: {exc}")
    if not jobs:
        pytest.skip("source returned nothing (down or failing)")
    print(module.__name__, len(jobs), {j.source: 0 for j in jobs}.keys())
    assert all(isinstance(j, Job) and j.title and j.url for j in jobs)
