"""Keyed collectors: run only in the local app, with the user's own free API keys.

Per-run call counts (10 search terms, one page each; no paging, no retries):
USAJOBS 10, Adzuna 10 per country (countries capped so total <= 20 of 250/day),
Careerjet 10 (+1 IP lookup), Jooble 10. Calls are paced TERM_DELAY seconds apart.
A 429 or any HTTP error aborts that source (logged, returns []).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from marine_jobs.models import DESCRIPTION_CAP, Job
from marine_jobs.sources.feeds import strip_html

log = logging.getLogger(__name__)

TERMS = [
    "marine biologist",
    "fisheries",
    "oceanography",
    "aquaculture",
    "marine technician",
    "coastal ecologist",
    "aquarist",
    "environmental scientist marine",
    "GIS analyst",
    "environmental technician",
]
TERM_DELAY = 1.0
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ATTRIBUTIONS = {"adzuna": "Jobs by Adzuna"}
ADZUNA_MAX_COUNTRIES = 2  # 10 terms * 2 = 20 calls <= 20


@dataclass
class KeyedSource:
    name: str
    env_vars: list[str]
    signup_url: str
    fetch: Callable[[httpx.Client, Mapping[str, str]], list[Job]]


def _date(value: Any) -> str:
    """ISO date from an ISO timestamp or RFC 2822 string; today if unparseable."""
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return datetime.now(UTC).date().isoformat()


def _text(html: Any) -> str:
    return strip_html(str(html or ""))[:DESCRIPTION_CAP]


def _bodies(client: httpx.Client, build: Callable[[str], httpx.Request]) -> list[Any]:
    """One paced request per search term; any HTTP error (incl. 429) aborts."""
    out = []
    for i, term in enumerate(TERMS):
        if i:
            time.sleep(TERM_DELAY)
        resp = client.send(build(term))
        resp.raise_for_status()
        out.append(resp.json())
    return out


def _dedup(jobs: list[Job]) -> list[Job]:
    return list({j.url: j for j in jobs}.values())


def _usajobs(client: httpx.Client, env: Mapping[str, str]) -> list[Job]:
    headers = {
        "Authorization-Key": env["USAJOBS_API_KEY"],
        "User-Agent": env["USAJOBS_EMAIL"],
    }

    def build(term: str) -> httpx.Request:
        params = {
            "Keyword": term,
            "PositionScheduleTypeCode": "1",
            "DatePosted": "30",
            "ResultsPerPage": "500",
        }
        return client.build_request(
            "GET", "https://data.usajobs.gov/api/search", params=params, headers=headers
        )

    jobs = []
    for body in _bodies(client, build):
        for item in body["SearchResult"]["SearchResultItems"]:
            d = item["MatchedObjectDescriptor"]
            details = d.get("UserArea", {}).get("Details", {})
            who = (details.get("WhoMayApply") or {}).get("Name", "")
            desc = details.get("JobSummary") or d.get("QualificationSummary", "")
            jobs.append(
                Job(
                    title=d["PositionTitle"],
                    company=d.get("OrganizationName", ""),
                    location="; ".join(
                        loc["LocationName"]
                        for loc in d.get("PositionLocation", [])
                        if loc.get("LocationName")
                    )
                    or d.get("PositionLocationDisplay")
                    or "",
                    url=d["PositionURI"],
                    source="usajobs",
                    posted=_date(d.get("PublicationStartDate")),
                    description=_text(desc),
                    # Competitive-service federal jobs require citizenship unless stated otherwise.
                    tags=[] if "non-citizen" in who.lower() else ["us-citizen"],
                )
            )
    return _dedup(jobs)


def _adzuna(client: httpx.Client, env: Mapping[str, str]) -> list[Job]:
    countries = [
        c.strip() for c in env.get("ADZUNA_COUNTRIES", "us").split(",") if c.strip()
    ]
    jobs = []
    for country in countries[:ADZUNA_MAX_COUNTRIES]:

        def build(term: str, country: str = country) -> httpx.Request:
            params = {
                "app_id": env["ADZUNA_APP_ID"],
                "app_key": env["ADZUNA_APP_KEY"],
                "what": term,
                "full_time": "1",
                "max_days_old": "30",
                "results_per_page": "50",
            }
            return client.build_request(
                "GET",
                f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
                params=params,
            )

        for body in _bodies(client, build):
            for r in body["results"]:
                jobs.append(
                    Job(
                        title=_text(r["title"]),
                        company=(r.get("company") or {}).get("display_name", ""),
                        location=(r.get("location") or {}).get("display_name", ""),
                        url=r["redirect_url"],
                        source="adzuna",
                        posted=_date(r.get("created")),
                        description=_text(r.get("description")),
                    )
                )
    return _dedup(jobs)


def _careerjet(client: httpx.Client, env: Mapping[str, str]) -> list[Job]:
    resp = client.get("https://api.ipify.org", timeout=10)
    resp.raise_for_status()
    ip = resp.text.strip()
    auth = httpx.BasicAuth(env["CAREERJET_API_KEY"], "")

    def build(term: str) -> httpx.Request:
        params = {
            "locale_code": "en_US",
            "keywords": term,
            "work_hours": "f",
            "page_size": "50",
            "page": "1",
            "sort": "date",
            "user_ip": ip,
            "user_agent": BROWSER_UA,
        }
        req = client.build_request(
            "GET", "https://search.api.careerjet.net/v4/query", params=params
        )
        return next(auth.auth_flow(req))

    jobs = []
    for body in _bodies(client, build):
        for r in body.get("jobs", []):
            jobs.append(
                Job(
                    title=_text(r["title"]),
                    company=r.get("company", ""),
                    location=r.get("locations", ""),
                    url=r["url"],
                    source="careerjet",
                    posted=_date(r.get("date")),
                    description=_text(r.get("description")),
                )
            )
    return _dedup(jobs)


def _jooble(client: httpx.Client, env: Mapping[str, str]) -> list[Job]:
    def build(term: str) -> httpx.Request:
        return client.build_request(
            "POST",
            f"https://jooble.org/api/{env['JOOBLE_API_KEY']}",
            json={"keywords": term, "location": ""},
        )

    jobs = []
    for body in _bodies(client, build):
        for r in body.get("jobs", []):
            jobs.append(
                Job(
                    title=_text(r["title"]),
                    company=r.get("company", ""),
                    location=r.get("location", ""),
                    url=r["link"],
                    source="jooble",
                    posted=_date(r.get("updated")),
                    description=_text(r.get("snippet")),
                )
            )
    return _dedup(jobs)


SOURCES: dict[str, KeyedSource] = {
    s.name: s
    for s in [
        KeyedSource(
            "usajobs",
            ["USAJOBS_API_KEY", "USAJOBS_EMAIL"],
            "https://developer.usajobs.gov/apirequest/",
            _usajobs,
        ),
        KeyedSource(
            "adzuna",
            ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"],
            "https://developer.adzuna.com/signup",
            _adzuna,
        ),
        KeyedSource(
            "careerjet",
            ["CAREERJET_API_KEY"],
            "https://www.careerjet.com/partners/api",
            _careerjet,
        ),
        KeyedSource(
            "jooble", ["JOOBLE_API_KEY"], "https://jooble.org/api/about", _jooble
        ),
    ]
}


def run_all(client: httpx.Client, env: Mapping[str, str]) -> list[Job]:
    jobs: list[Job] = []
    for src in SOURCES.values():
        if not all(env.get(v) for v in src.env_vars):
            continue
        try:
            jobs.extend(src.fetch(client, env))
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:  # isolate one broken source
            log.warning("keyed source %s failed: %s", src.name, type(exc).__name__)
    return jobs
