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
    r"pinnipeds?|noaa|nmfs|great lakes|limnolog\w*|underwater|offshore wind|blue carbon)\b",
    re.IGNORECASE,
)

# Roles open to marine-science grads outside ocean work. Matched on the title only, since
# words like "environmental" or "conservation" show up in unrelated postings' boilerplate.
ADJACENT_TITLE_RE = re.compile(
    r"\b(?:gis|geospatial|remote sensing|cartograph\w*|environmental|ecolog\w*|"
    r"biologists?|aquatic|wildlife|natural resources?|conservation|water quality|water resources|"
    r"hydrolog\w*|watershed|stormwater|wetland\w*|habitat|restoration|invasive species|"
    r"field (?:tech\w*|crew|assistant)|nepa|sustainability|climate)\b",
    re.IGNORECASE,
)
# Adjacent-title matches that are safety, mechanic, or lab-biology roles.
ADJACENT_EXCLUDE_RE = re.compile(
    r"\b(?:health (?:and|&) safety|ehs|mechanic|computational|molecular)\b",
    re.IGNORECASE,
)
# Roles a marine-science bachelor's does not qualify for, even at ocean employers: business
# (marine insurance, ocean freight, sales), engineering and software, ship's crew and galley
# (licensed), hospitality and warehouse, plant operators, land surveying, other degrees
# (archaeology, chemistry), and internships, which are usually for current students.
NON_SCIENCE_TITLE_RE = re.compile(
    r"\b(?:sales|adjuster|underwriter|insurance|freight|import|export|tutor|legal|attorney|"
    r"paralegal|philanthropy|fundrais\w*|accountant|receivable|recruiter|engineer\w*|"
    r"developer|software|programmer|chef|cook|galley|(?<!land )steward|deckhand|mate|seafarer|oiler|"
    r"qmed|licensed|host|driver|packer|facilities|logistics|composite|membrane|wastewater|"
    r"pump|filtration|law enforcement|archaeolog\w*|paleontolog\w*|chemist|survey field tech\w*|"
    r"distribution|adjunct|intern|internship)\b",
    re.IGNORECASE,
)
# Broad aggregators whose postings come from any industry: match relevance on the title only.
TITLE_ONLY_SOURCES = {"himalayas"}

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
    r"vice president|vp|staff (?:scientist|engineer)|managing|partner|leader|professor|"
    r"faculty|instructor|lecturer|fellow|volunteer|work study|supervisor|curator|expert|"
    r"intermediate|middle)\b|\bpost-?doc",
    re.IGNORECASE,
)

TEMP_RE = re.compile(
    rf"\b(?:seasonal|temporary|limited[- ]term|fixed[- ]term|term[- ]limited|"
    r"term (?:position|appointment|employee|role|job)|not[- ]to[- ]exceed|"
    rf"contract (?:position|role|job|employee|basis)|contractor|{_NUM}[- ]months?\b)",
    re.IGNORECASE,
)
# Postings that name a degree a marine-science graduate holds or that counts as "related".
DEGREE_FIT_RE = re.compile(
    r"(?:\b(?:bachelor\w*|undergraduate|degree)\b|\bb\.?s\.?c?(?![a-z])|\bb\.?a\.?(?![a-z]))"
    r"[^.;\n]{0,80}?\b(?:"
    r"marine (?:science|biology|ecology)|ocean(?:ography| science)|oceanograph\w*|fisheries|"
    r"aquatic (?:science|biology|ecology)|biolog(?:y|ical sciences?)|ecology|"
    r"environmental (?:science|studies)|natural resources?|wildlife|zoology|geography|"
    r"earth sciences?|geosciences?)",
    re.IGNORECASE,
)
CITIZEN_RE = re.compile(
    r"\b(?:u\.?s\.? citizen\w*|united states citizen\w*|citizenship (?:is )?required|"
    r"must be a citizen|security clearance|secret clearance|clearance (?:is )?required)",
    re.IGNORECASE,
)

# Fake-job red flags (FTC job-scam guidance): off-platform chat, personal email, money moves.
SCAM_RE = re.compile(
    r"\b(?:telegram|whats ?app|wickr|signal app|google hangouts|"
    r"[\w.+-]+@(?:gmail|yahoo|hotmail|outlook|aol|icloud|protonmail)\.com|"
    r"text (?:us |me )?(?:to apply|your (?:resume|cv|name))|"
    r"(?:cashier'?s? )?(?:check|cheque)s? (?:to deposit|will be (?:sent|mailed))|"
    r"deposit (?:the |a )?(?:check|cheque)|reship\w*|package forwarding|"
    r"(?:training|registration|application|processing|onboarding|starter kit) fee|"
    r"purchase (?:your own )?(?:equipment|software) (?:from|through)|"
    r"wire transfer|gift cards?|bitcoin|zelle|cash ?app|venmo)",
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
    if (
        SENIOR_TITLE_RE.search(job.title)
        or NON_SCIENCE_TITLE_RE.search(job.title)
        or DEGREE_REQUIRED_RE.search(text)
    ):
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


US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
    "PR",
}
US_RE = re.compile(
    r"united states|\busa\b|\bu\.s\.|\bUS\b|\bUS-[A-Z]{2}\b|alabama|alaska|arizona|arkansas|"
    r"california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|"
    r"iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
    r"new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
    r"south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"wisconsin|wyoming|puerto rico",
    re.IGNORECASE,
)
# ponytail: hand-picked country list, not exhaustive; unlisted countries count as unknown and stay.
NON_US_RE = re.compile(
    r"\b(?:oconus|canada|mexico|brazil|chile|peru|colombia|argentina|ecuador|"
    r"united kingdom|england|scotland|wales|ireland|france|germany|spain|portugal|italy|"
    r"netherlands|belgium|denmark|norway|sweden|finland|iceland|poland|romania|greece|"
    r"switzerland|austria|australia|new zealand|india|china|japan|korea|singapore|malaysia|"
    r"indonesia|philippines|thailand|vietnam|taiwan|hong kong|egypt|south africa|kenya|"
    r"mozambique|nigeria|saudi arabia|united arab emirates|uae|qatar|oman|israel|turkey|"
    r"micronesia|fiji|bahamas|london|bangalore|bengaluru|gurugram|gurgaon|hyderabad|noida|"
    r"mumbai|pune|chennai|kolkata|delhi)\b|[,(]\s*(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\b",
    re.IGNORECASE,
)
REGION_COUNTRY_RE = re.compile(
    r",[^,]+,\s*([A-Z]{2})\s*$"
)  # "Nantes, Pays de la Loire, FR"
CITY_CODE_RE = re.compile(r",\s*([A-Z]{2})\s*$")  # "Tampa, FL" or "Milan, IT"


def _non_us(place: str) -> bool:
    """True only when the place is clearly outside the US; unknown places count as US."""
    if m := REGION_COUNTRY_RE.search(place):
        return m[1] != "US"
    if US_RE.search(place):
        return False
    foreign = bool(NON_US_RE.search(place))
    if m := CITY_CODE_RE.search(place):
        # "IN" is Indiana or India; the city name decides ("Bangalore, IN").
        return m[1] not in US_STATES or (m[1] == "IN" and foreign)
    return foreign


CJK_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")


PAY_FLOOR_USD = 40_000
FX_TO_USD = {
    "$": 1.0,
    "£": 1.27,
    "€": 1.08,
}  # ponytail: fixed rates; refresh if they drift far
PAY_RE = re.compile(
    r"([$£€])\s?(\d[\d,]*(?:\.\d+)?)\s?(k\b)?"
    r"(?:\s*(?:-|–|to)\s*[$£€]?\s?(\d[\d,]*(?:\.\d+)?)\s?(k\b)?)?"
    r"\s*(?:/\s*|per\s+|an?\s+)(hour|hr|year|yr|annum)",
    re.IGNORECASE,
)


def _annual_pay(text: str) -> float | None:
    """Highest annualized USD pay stated in the text, or None when no pay is listed."""
    best = None
    for cur, low, low_k, high, high_k, period in PAY_RE.findall(text):
        amount = float((high or low).replace(",", "")) * (
            1000 if (high_k or low_k) else 1
        )
        if period.lower() in {"hour", "hr"}:
            amount *= 2080
        amount *= FX_TO_USD[cur]
        best = amount if best is None else max(best, amount)
    return best


def _location_ok(job: Job) -> bool:
    """US jobs (or unknown location) always; elsewhere only remote, and not below the pay floor."""
    # Some boards hide the country in the title ("Intern Hiring - China", Japanese titles).
    foreign_title = NON_US_RE.search(job.title) or CJK_RE.search(job.title)
    if not foreign_title and not all(_non_us(part) for part in job.location.split(";")):
        return True
    if not (job.remote or "remote" in job.location.lower()):
        return False
    pay = _annual_pay(job.description)
    return pay is None or pay >= PAY_FLOOR_USD


def classify(jobs: list[Job]) -> list[Job]:
    out = []
    for job in jobs:
        marine = (
            job.company.lower() in MARINE_EMPLOYERS
            or MARINE_RE.search(
                job.title if job.source in TITLE_ONLY_SOURCES else _text(job)
            )
            or (
                ADJACENT_TITLE_RE.search(job.title)
                and not ADJACENT_EXCLUDE_RE.search(job.title)
            )
        )
        if (
            not marine
            or _excluded(job)
            or SCAM_RE.search(_text(job))
            or not _location_ok(job)
        ):
            continue
        text = _text(job)
        tags = [
            t
            for t, rx in (
                ("temp", TEMP_RE),
                ("us-citizen", CITIZEN_RE),
                ("degree-fit", DEGREE_FIT_RE),
            )
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
