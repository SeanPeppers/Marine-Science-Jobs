"""Classify, dedup, and merge jobs into the rolling store."""

from __future__ import annotations

import json
import re
import string
from dataclasses import replace
from datetime import date
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from marine_jobs.models import Job

MAX_AGE_DAYS = 60
DUP_THRESHOLD = 90
COMPANY_THRESHOLD = 85
EMPLOYERS = Path(__file__).resolve().parents[2] / "data" / "employers.yaml"
# Employers flagged `marine: true` pass the relevance filter even with no description text.
MARINE_EMPLOYERS = {
    e["name"].lower() for e in yaml.safe_load(EMPLOYERS.read_text()) if e.get("marine")
}
LEVEL_RE = re.compile(r"\b(?:i{1,3}|iv|v|[1-5])\b", re.IGNORECASE)
ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}

MARINE_RE = re.compile(
    r"\b(?:ocean\w*|marine|maritime ecolog\w*|sea ?grant|seafood|seafloor|subsea|"
    r"sea turtles?|sea ?grass\w*|deep[- ]sea|fish\w*|aquacultur\w*|mariculture|"
    r"shellfish|oysters?|coastal|coasts?|estuar\w*|reefs?|coral\w*|kelp|mangroves?|"
    r"salt ?marsh\w*|wetlands?|tidal|intertidal|aquariums?|hydrograph\w*|bathymetr\w*|"
    r"benthic|plankton\w*|phytoplankton|zooplankton|whales?|dolphins?|cetaceans?|"
    r"pinnipeds?|noaa|nmfs|great lakes|limnolog\w*|underwater|offshore wind)\b",
    re.IGNORECASE,
)

WORD_NUMBERS = {
    w: i
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
        ]
    )
}
_NUM = r"(\d+|" + "|".join(WORD_NUMBERS) + r")"

_DEGREE = (
    r"(?:master[’']?s|m\.\s?s\.|\bms degree|m\.?sc\b|ph\.?\s?d\b|doctora(?:te|l)|"
    r"\bgraduate degree|advanced degree)"
)
DEGREE_REQUIRED_RE = re.compile(
    rf"{_DEGREE}(?:(?!prefer)[^.;\n]){{0,40}}?\b(?:is |are )?required|"
    rf"(?:requires?|required:?|must (?:have|hold|possess))\s+(?:an?\s+)?{_DEGREE}",
    re.IGNORECASE,
)
MIN_YEARS = 3
# Lower bound of "N", "N+", "N-M", "minimum of N", "at least N" years, followed by experience.
YEARS_RE = re.compile(
    rf"{_NUM}(?:\s*(?:-|–|to)\s*{_NUM})?\s*\+?\s*(?:or more\s+)?years?\b"
    r"[^.;\n]{0,60}?experience",
    re.IGNORECASE,
)
SENIOR_TITLE_RE = re.compile(
    r"\b(?:senior|supervisory|sr\b\.?|lead|principal|manager|director|head of|chief|"
    r"vice president|vp|staff (?:scientist|engineer))\b|\bpost-?doc",
    re.IGNORECASE,
)

TEMP_RE = re.compile(
    rf"\b(?:seasonal|temporary|limited[- ]term|fixed[- ]term|term[- ]limited|"
    r"term (?:position|appointment|employee|role|job)|not[- ]to[- ]exceed|"
    rf"contract (?:position|role|job|employee|basis)|contractor|{_NUM}[- ]months?\b)",
    re.IGNORECASE,
)
CITIZEN_RE = re.compile(
    r"\b(?:u\.?s\.? citizen\w*|united states citizen\w*|citizenship (?:is )?required|"
    r"must be a citizen|security clearance|secret clearance|clearance (?:is )?required)",
    re.IGNORECASE,
)

SUBFIELDS: dict[str, list[str]] = {
    "marine biology": [
        "marine biolog",
        "marine mammal",
        "whale",
        "dolphin",
        "sea turtle",
        "species",
        "biologist",
        "ecology",
        "invertebrate",
        "seabird",
    ],
    "oceanography": [
        "oceanograph",
        "physical ocean",
        "ocean circulation",
        "biogeochem",
        "ctd",
        "ocean observ",
        "glider",
        "argo",
        "climate",
    ],
    "fisheries": [
        "fisher",
        "fish",
        "stock assessment",
        "observer",
        "nmfs",
        "salmon",
        "trawl",
    ],
    "aquaculture": [
        "aquacultur",
        "mariculture",
        "hatchery",
        "oyster",
        "shellfish",
        "seaweed",
        "farm",
    ],
    "coastal & estuarine ecology": [
        "coastal",
        "estuar",
        "wetland",
        "marsh",
        "mangrove",
        "seagrass",
        "intertidal",
        "reef",
        "coral",
        "restoration",
    ],
    "conservation & policy": [
        "conservation",
        "policy",
        "management plan",
        "regulat",
        "sea grant",
        "advocacy",
        "outreach",
        "protected area",
        "permit",
    ],
    "ocean engineering & technology": [
        "engineer",
        "robot",
        "autonomous",
        "rov",
        "auv",
        "sensor",
        "instrument",
        "subsea",
        "offshore wind",
        "hardware",
        "electronic",
    ],
    "marine data & GIS": [
        "gis",
        "data",
        "remote sensing",
        "satellite",
        "hydrograph",
        "bathymetr",
        "mapping",
        "python",
        "model",
        "analyst",
    ],
    "education & aquarium": [
        "aquarium",
        "educat",
        "teach",
        "interpret",
        "curator",
        "husbandry",
        "aquarist",
        "visitor",
        "camp",
    ],
    "environmental consulting": [
        "consult",
        "environmental scien",
        "compliance",
        "impact",
        "assessment",
        "nepa",
        "field technician",
        "survey",
    ],
}
TITLE_WEIGHT = 3


def _text(job: Job) -> str:
    return f"{job.title}\n{job.description}"


def _min_years(match: re.Match[str]) -> int:
    first = match.group(1).lower()
    return int(first) if first.isdigit() else WORD_NUMBERS[first]


def _excluded(job: Job) -> bool:
    text = _text(job)
    if SENIOR_TITLE_RE.search(job.title) or DEGREE_REQUIRED_RE.search(text):
        return True
    return any(_min_years(m) >= MIN_YEARS for m in YEARS_RE.finditer(text))


_SUBFIELD_RES = {
    name: re.compile(r"\b(?:" + "|".join(map(re.escape, kws)) + ")", re.IGNORECASE)
    for name, kws in SUBFIELDS.items()
}


def _subfield(job: Job) -> str:
    scores = {
        name: TITLE_WEIGHT * len(rx.findall(job.title))
        + len(rx.findall(job.description))
        for name, rx in _SUBFIELD_RES.items()
    }
    best = max(scores, key=lambda name: scores[name])
    return best if scores[best] else "other"


def classify(jobs: list[Job]) -> list[Job]:
    out = []
    for job in jobs:
        marine = job.company.lower() in MARINE_EMPLOYERS or MARINE_RE.search(_text(job))
        if not marine or _excluded(job):
            continue
        text = _text(job)
        tags = [
            t
            for t, rx in (("temp", TEMP_RE), ("us-citizen", CITIZEN_RE))
            if rx.search(text)
        ]
        merged = sorted(set(job.tags) | set(tags))  # keep tags a source set itself
        out.append(replace(job, subfield=_subfield(job), tags=merged))
    return out


_PUNCT = str.maketrans(string.punctuation, " " * len(string.punctuation))
_SUFFIXES = {
    "inc",
    "llc",
    "ltd",
    "co",
    "corp",
    "corporation",
    "company",
    "plc",
    "gmbh",
    "the",
}


def _norm(value: str) -> str:
    return " ".join(
        w for w in value.lower().translate(_PUNCT).split() if w not in _SUFFIXES
    )


def _key(job: Job) -> str:
    return f"{_norm(job.company)} {_norm(job.title)} {_norm(job.location)}"


def _same(a: Job, b: Job) -> bool:
    if a.url == b.url:
        return True
    if _levels(a.title) != _levels(b.title):
        return False
    return fuzz.token_set_ratio(
        _norm(a.company), _norm(b.company)
    ) >= COMPANY_THRESHOLD and (
        fuzz.token_sort_ratio(_key(a), _key(b)) >= DUP_THRESHOLD
    )


def _levels(title: str) -> set[str]:
    """Grade suffixes ("Biologist II", "Tech 3") mark distinct openings, never duplicates."""
    return {ROMAN.get(m.lower(), m) for m in LEVEL_RE.findall(title)}


def _combine(a: Job, b: Job) -> Job:
    keep = a if len(a.description) >= len(b.description) else b
    first_seen = min((d for d in (a.first_seen, b.first_seen) if d), default="")
    return replace(keep, posted=min(a.posted, b.posted), first_seen=first_seen)


def _find(pool: list[Job], job: Job) -> int | None:
    return next((i for i, other in enumerate(pool) if _same(other, job)), None)


def dedup(jobs: list[Job]) -> list[Job]:
    # ponytail: O(n²) scan; bucket by normalized company if job counts reach the tens of thousands.
    out: list[Job] = []
    for job in jobs:
        i = _find(out, job)
        if i is None:
            out.append(job)
        else:
            out[i] = _combine(out[i], job)
    return out


def merge(
    store: list[Job], fresh: list[Job], today: str, sources_run: set[str]
) -> list[Job]:
    remaining = list(store)
    out = []
    for job in fresh:
        i = _find(remaining, job)
        first_seen = remaining.pop(i).first_seen if i is not None else ""
        out.append(
            replace(job, first_seen=first_seen or today, last_seen=today, closed=False)
        )
    for job in remaining:
        ran = job.source in sources_run or f"{job.source}:{job.company}" in sources_run
        out.append(replace(job, closed=True) if ran else job)
    cutoff = date.fromisoformat(today).toordinal() - MAX_AGE_DAYS
    return [
        j for j in out if date.fromisoformat(j.last_seen or today).toordinal() >= cutoff
    ]


def load_store(path: Path) -> list[Job]:
    if not path.exists():
        return []
    return [Job.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]


def sort_newest(jobs: list[Job]) -> list[Job]:
    return sorted(jobs, key=lambda j: (j.posted, j.id), reverse=True)


def save_store(path: Path, jobs: list[Job]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [j.to_dict() for j in sort_newest(jobs)]
    path.write_text(
        json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
