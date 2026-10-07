"""Keyless collectors: Conservation Job Board, ECO Magazine, Canada Job Bank, Himalayas."""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any

import feedparser  # type: ignore[import-untyped]
import httpx

from marine_jobs.models import DESCRIPTION_CAP, Job

log = logging.getLogger(__name__)

UA = "marine-jobs/0.1 (personal job search)"
CJB_URL = "https://www.conservationjobboard.com/rss"
ECO_URL = "https://ecomagazine.com/feed/?post_type=job"
JOBBANK_URL = (
    "https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed?searchstring={}"
)
JOBBANK_TERMS = [
    "biologist",
    "fisheries",
    "marine",
    "aquaculture",
    "oceanographer",
    "hydrographer",
    "GIS",
    "environmental technician",
    "water quality",
]
JOBBANK_DELAY = 5  # robots.txt Crawl-delay
# Himalayas public remote-jobs API: reuse allowed with a link back and source credit, which
# the board's Apply link and README credit provide. Data refreshes daily, so one page per term.
HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
HIMALAYAS_TERMS = [
    "marine",
    "ocean",
    "fisheries",
    "GIS",
    "geospatial",
    "remote sensing",
    "environmental scientist",
    "environmental technician",
    "ecologist",
    "biologist",
    "wildlife",
    "conservation",
    "water quality",
    "hydrologist",
]
HIMALAYAS_DELAY = 2
HIMALAYAS_SKIP_SENIORITY = {"Senior", "Manager", "Director", "Executive", "Lead"}
CURRENCY_SIGN = {"USD": "$", "GBP": "£", "EUR": "€"}


def today() -> str:
    return datetime.now(UTC).date().isoformat()


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("br", "p", "li", "div"):
            self.parts.append("\n")


def strip_html(html: str) -> str:
    parser = _Text()
    parser.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _posted(entry: Any) -> str:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    return date(*st[:3]).isoformat() if st else today()


def _get(client: httpx.Client, url: str) -> Any:
    resp = client.get(url, headers={"User-Agent": UA}, timeout=30)
    resp.raise_for_status()
    return feedparser.parse(resp.content)


def _cjb(client: httpx.Client) -> list[Job]:
    jobs = []
    for e in _get(client, CJB_URL).entries:
        # description: "<title><br>$salary -- Organization<br>"
        lines = strip_html(e.get("summary", "")).splitlines()
        company = (
            lines[1].split(" -- ", 1)[1].strip()
            if len(lines) > 1 and " -- " in lines[1]
            else ""
        )
        # url slug ends with "-<city>-<state>/<id>"; location left to the description
        jobs.append(
            Job(
                title=e.title.strip(),
                company=company,
                location="",
                url=e.link,
                source="conservationjobboard",
                posted=_posted(e),
                description="\n".join(lines)[:DESCRIPTION_CAP],
            )
        )
    return jobs


def _eco(client: httpx.Client) -> list[Job]:
    jobs = []
    for e in _get(client, ECO_URL).entries:
        if (
            "/news/" in e.link
        ):  # the post_type filter is ignored by the site; drop news articles
            continue
        text = strip_html(e.get("summary", ""))
        jobs.append(
            Job(
                title=e.title.strip(),
                company="",
                location="",
                url=e.link,
                source="ecomagazine",
                posted=_posted(e),
                description=text[:DESCRIPTION_CAP],
            )
        )
    return jobs


def _jobbank(client: httpx.Client) -> list[Job]:
    seen: dict[str, Job] = {}
    for i, term in enumerate(JOBBANK_TERMS):
        if i:
            time.sleep(JOBBANK_DELAY)
        try:
            entries = _get(client, JOBBANK_URL.format(term)).entries
        except httpx.HTTPError as exc:
            log.warning("jobbank %r failed: %s", term, exc)
            continue
        for e in entries:
            text = strip_html(e.get("summary", ""))
            field = {
                k.strip(): v.strip()
                for k, _, v in (ln.partition(":") for ln in text.splitlines())
            }
            seen.setdefault(
                e.link,
                Job(
                    title=e.title.strip(),
                    company=field.get("Employer", ""),
                    location=field.get("Location", ""),
                    url=e.link,
                    source="jobbank",
                    posted=_posted(e),
                    description=text[:DESCRIPTION_CAP],
                    remote="remote" in text.lower(),
                ),
            )
    return list(seen.values())


def _pay_line(j: dict[str, Any]) -> str:
    """'Pay: $50000 - $70000 per year' so the pipeline's pay floor can read it."""
    sign = CURRENCY_SIGN.get(j.get("currency") or "USD")
    top = j.get("maxSalary") or j.get("minSalary")
    if not sign or not top:
        return ""
    period = "hour" if j.get("salaryPeriod") == "hourly" else "year"
    return f"Pay: {sign}{j.get('minSalary') or top} - {sign}{top} per {period}\n"


def _himalayas(client: httpx.Client) -> list[Job]:
    seen: dict[str, Job] = {}
    for i, term in enumerate(HIMALAYAS_TERMS):
        if i:
            time.sleep(HIMALAYAS_DELAY)
        resp = client.get(
            HIMALAYAS_URL, params={"q": term}, headers={"User-Agent": UA}, timeout=30
        )
        resp.raise_for_status()  # a 429 aborts the source; the next daily run retries
        for j in resp.json()["jobs"]:
            if HIMALAYAS_SKIP_SENIORITY & set(j.get("seniority") or []):
                continue
            where = ", ".join(j.get("locationRestrictions") or []) or "Anywhere"
            seen.setdefault(
                j["guid"],
                Job(
                    title=j["title"].strip(),
                    company=j.get("companyName", ""),
                    location=f"Remote - {where}",
                    url=j["guid"],
                    source="himalayas",
                    posted=datetime.fromtimestamp(j["pubDate"], UTC).date().isoformat(),
                    description=(_pay_line(j) + strip_html(j.get("description", "")))[
                        :DESCRIPTION_CAP
                    ],
                    remote=True,
                ),
            )
    return list(seen.values())


def fetch(client: httpx.Client) -> list[Job]:
    jobs: list[Job] = []
    for name, collector in (
        ("conservationjobboard", _cjb),
        ("ecomagazine", _eco),
        ("jobbank", _jobbank),
        ("himalayas", _himalayas),
    ):
        try:
            got = collector(client)
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            log.warning("%s failed: %s", name, exc)
            continue
        log.info("%s: %d jobs", name, len(got))
        jobs += got
    return jobs
