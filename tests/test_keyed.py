import os

import httpx
import pytest

from marine_jobs.sources.keyed import SOURCES, run_all


def test_empty_env_makes_no_calls() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected call")

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        assert run_all(client, {}) == []


@pytest.mark.parametrize("name", list(SOURCES))
def test_live(name: str) -> None:
    src = SOURCES[name]
    if not all(os.environ.get(v) for v in src.env_vars):
        pytest.skip(f"{src.env_vars} not set")
    with httpx.Client(timeout=30) as client:
        jobs = src.fetch(client, os.environ)
    assert jobs
    for j in jobs[:5]:
        assert j.title and j.url and j.posted and j.source == name
