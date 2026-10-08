"""Public ATS job-board collectors, driven by data/employers.yaml."""

from __future__ import annotations

import html
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml

from marine_jobs.models import DESCRIPTION_CAP, Job
from marine_jobs.sources.feeds import UA, strip_html, today

log = logging.getLogger(__name__)

EMPLOYERS = Path(__file__).resolve().parents[3] / "data" / "employers.yaml"
PAUSE = 1.0  # seconds between requests
MAX_PAGES = 50  # hard stop for pagination loops


def _text(value: str) -> str:
    return strip_html(html.unescape(value or ""))[:DESCRIPTION_CAP]


def _iso(value: Any) -> str:
    """ISO datetime string or epoch milliseconds -> YYYY-MM-DD; today when missing."""
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, UTC).date().isoformat()
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return today()


def _is_remote(*texts: str) -> bool:
    return any("remote" in t.lower() for t in texts if t)


def _get(client: httpx.Client, url: str, **params: Any) -> Any:
    time.sleep(PAUSE)
    resp = client.get(
        url, params=params or None, headers={"User-Agent": UA}, timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def _greenhouse(client: httpx.Client, e: dict[str, Any]) -> list[Job]:
    data = _get(
        client,
        f"https://boards-api.greenhouse.io/v1/boards/{e['slug']}/jobs",
        content="true",
    )
    jobs = []
    for j in data["jobs"]:
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(
            Job(j["title"], e["name"], loc, j["absolute_url"], "greenhouse", _iso(j.get("updated_at")),
                _text(j.get("content", "")), _is_remote(loc))
        )  # fmt: skip
    return jobs


def _lever(client: httpx.Client, e: dict[str, Any]) -> list[Job]:
    jobs = []
    for j in _get(client, f"https://api.lever.co/v0/postings/{e['slug']}", mode="json"):
        loc = (j.get("categories") or {}).get("location") or ""
        remote = j.get("workplaceType") == "remote" or _is_remote(loc)
        jobs.append(
            Job(j["text"], e["name"], loc, j["hostedUrl"], "lever", _iso(j.get("createdAt")),
                (j.get("descriptionPlain") or "")[:DESCRIPTION_CAP], remote)
        )  # fmt: skip
    return jobs


def _ashby(client: httpx.Client, e: dict[str, Any]) -> list[Job]:
    data = _get(client, f"https://api.ashbyhq.com/posting-api/job-board/{e['slug']}")
    jobs = []
    for j in data["jobs"]:
        loc = j.get("location") or ""
        remote = bool(j.get("isRemote")) or j.get("workplaceType") == "Remote"
        jobs.append(
            Job(j["title"], e["name"], loc, j["jobUrl"], "ashby", _iso(j.get("publishedAt")),
                (j.get("descriptionPlain") or "")[:DESCRIPTION_CAP], remote)
        )  # fmt: skip
    return jobs


def _smartrecruiters(client: httpx.Client, e: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for page in range(MAX_PAGES):
        data = _get(client, f"https://api.smartrecruiters.com/v1/companies/{e['slug']}/postings",
                    limit=100, offset=page * 100)  # fmt: skip
        for j in data["content"]:
            loc = j.get("location") or {}
            place = ", ".join(
                p
                for p in (
                    loc.get("city"),
                    loc.get("region"),
                    loc.get("country", "").upper(),
                )
                if p
            )
            url = f"https://jobs.smartrecruiters.com/{e['slug']}/{j['id']}"
            jobs.append(
                Job(j["name"], e["name"], place, url, "smartrecruiters", _iso(j.get("releasedDate")),
                    "", bool(loc.get("remote")))
            )  # fmt: skip
        if not data["content"] or len(jobs) >= data.get("totalFound", 0):
            break
    return jobs


def _workday_date(posted_on: str) -> str:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago'."""
    m = re.search(r"(\d+)\+? days", posted_on, re.IGNORECASE)
    days = int(m[1]) if m else 1 if "yesterday" in posted_on.lower() else 0
    return (datetime.now(UTC).date() - timedelta(days=days)).isoformat()


def _workday(client: httpx.Client, e: dict[str, Any]) -> list[Job]:
    url = f"https://{e['host']}/wday/cxs/{e['tenant']}/{e['site']}/jobs"
    jobs: dict[str, Job] = {}
    total = 0
    for page in range(MAX_PAGES):
        time.sleep(PAUSE)
        body = {"limit": 20, "offset": page * 20, "searchText": "", "appliedFacets": {}}
        resp = client.post(
            url, json=body, headers={"User-Agent": UA}, timeout=30
        )  # read-only search POST
        resp.raise_for_status()
        data = resp.json()
        postings = data["jobPostings"]
        # Workday reports `total` only on the first page; later pages send 0.
        total = total or data.get("total") or 0
        for j in postings:
            if "title" not in j or "externalPath" not in j:  # un-linkable stubs
                continue
            loc = j.get("locationsText", "")
            link = f"https://{e['host']}/{e['site']}{j['externalPath']}"
            jobs[link] = Job(j["title"], e["name"], loc, link, "workday",
                             _workday_date(j.get("postedOn", "")), "", _is_remote(loc))  # fmt: skip
        if len(postings) < 20 or (total and len(jobs) >= total):
            break
    return list(jobs.values())


LOCATIONS_COUNT_RE = re.compile(r"^\d+ Locations$")


def _workday_detail(client: httpx.Client, job: Job, e: dict[str, Any]) -> Job:
    path = job.url.split(f"/{e['site']}", 1)[1]
    info = _get(
        client, f"https://{e['host']}/wday/cxs/{e['tenant']}/{e['site']}{path}"
    )["jobPostingInfo"]
    country = (info.get("country") or {}).get("descriptor", "")
    # "3 Locations" hides where the job is; the posting's country is the best stand-in.
    location = (
        country if country and LOCATIONS_COUNT_RE.match(job.location) else job.location
    )
    remote = job.remote or "remote" in str(info.get("remoteType", "")).lower()
    return replace(job, description=_text(info.get("jobDescription", "")),
                   location=location, remote=remote)  # fmt: skip


def _smartrecruiters_detail(client: httpx.Client, job: Job, e: dict[str, Any]) -> Job:
    posting_id = job.url.rsplit("/", 1)[1]
    sections = _get(
        client,
        f"https://api.smartrecruiters.com/v1/companies/{e['slug']}/postings/{posting_id}",
    )["jobAd"]["sections"]
    parts = (
        sections.get(k, {}).get("text", "")
        for k in ("jobDescription", "qualifications")
    )
    return replace(job, description=_text("\n".join(parts)))


DETAILS = {"workday": _workday_detail, "smartrecruiters": _smartrecruiters_detail}


def employers() -> list[dict[str, Any]]:
    return yaml.safe_load(EMPLOYERS.read_text()) or []


def describe(client: httpx.Client, jobs: list[Job]) -> list[Job]:
    """Fill descriptions that list endpoints omit (Workday, SmartRecruiters).

    Run on already-classified jobs only: one request per job, so classify first keeps the
    count to the relevant few rather than every posting on large boards.
    """
    by_name = {e["name"]: e for e in employers()}
    out = []
    for job in jobs:
        detail = DETAILS.get(job.source)
        e = by_name.get(job.company)
        if detail and e and not job.description:
            try:
                job = detail(client, job, e)
            except Exception as exc:  # noqa: BLE001 - keep the job without a description
                log.warning("%s detail failed for %s: %s", job.source, job.url, exc)
        out.append(job)
    return out


COLLECTORS: dict[str, Callable[[httpx.Client, dict[str, Any]], list[Job]]] = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "smartrecruiters": _smartrecruiters,
    "workday": _workday,
}


def fetch(client: httpx.Client) -> list[Job]:
    jobs: list[Job] = []
    for e in employers():
        try:
            got = COLLECTORS[e["ats"]](client, e)
        except Exception as exc:  # noqa: BLE001 - one bad employer must not stop the run
            log.warning("%s (%s) failed: %s", e.get("name"), e.get("ats"), exc)
            continue
        log.info("%s: %d jobs", e["name"], len(got))
        jobs += got
    return jobs
