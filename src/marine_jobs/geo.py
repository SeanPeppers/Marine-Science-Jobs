"""Offline geocoding from GeoNames cities1000 (CC-BY 4.0, https://www.geonames.org/)."""

from __future__ import annotations

import io
import math
import re
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx

CACHE_DIR = Path.home() / ".cache" / "marine-jobs"
BASE_URL = "https://download.geonames.org/export/dump/"
# GeoNames admin1 codes are postal abbreviations only for the US; elsewhere they are numeric.
LETTER_CODE_COUNTRIES = {"US"}
EARTH_RADIUS_MILES = 3958.8


@dataclass(frozen=True)
class City:
    name: str
    ascii_name: str
    admin1_code: str
    admin1_name: str
    country: str
    country_name: str
    lat: float
    lon: float
    population: int

    @property
    def label(self) -> str:
        region = (
            self.admin1_code
            if self.country in LETTER_CODE_COUNTRIES
            else self.admin1_name
        )
        return ", ".join(p for p in (self.name, region, self.country) if p)


def _fetch(name: str) -> bytes:
    path = CACHE_DIR / name
    if not path.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        resp = httpx.get(BASE_URL + name, timeout=60, follow_redirects=True)
        resp.raise_for_status()
        tmp = path.with_suffix(".part")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
    return path.read_bytes()


def _rows(text: str) -> list[list[str]]:
    return [
        line.split("\t")
        for line in text.splitlines()
        if line and not line.startswith("#")
    ]


@lru_cache(maxsize=1)
def _cities() -> list[City]:
    with zipfile.ZipFile(io.BytesIO(_fetch("cities1000.zip"))) as zf:
        city_text = zf.read("cities1000.txt").decode("utf-8")
    admin1 = {r[0]: r[2] for r in _rows(_fetch("admin1CodesASCII.txt").decode("utf-8"))}
    countries = {r[0]: r[4] for r in _rows(_fetch("countryInfo.txt").decode("utf-8"))}
    cities = [
        City(
            name=r[1],
            ascii_name=r[2],
            admin1_code=r[10],
            admin1_name=admin1.get(f"{r[8]}.{r[10]}", ""),
            country=r[8],
            country_name=countries.get(r[8], ""),
            lat=float(r[4]),
            lon=float(r[5]),
            population=int(r[14] or 0),
        )
        for r in _rows(city_text)
    ]
    cities.sort(key=lambda c: -c.population)
    return cities


@lru_cache(maxsize=1)
def _by_name() -> dict[str, list[City]]:
    index: dict[str, list[City]] = {}
    for c in _cities():
        for key in {c.name.lower(), c.ascii_name.lower()}:
            index.setdefault(key, []).append(c)
    return index


def search_cities(prefix: str, limit: int = 10) -> list[City]:
    """Most populous cities whose name starts with prefix."""
    p = prefix.strip().lower()
    if not p:
        return []
    # ponytail: linear scan over ~30k cities, fine for typing speed; add a sorted index if slow.
    out = [
        c
        for c in _cities()
        if c.ascii_name.lower().startswith(p) or c.name.lower().startswith(p)
    ]
    return out[:limit]


_COUNTRY_ALIASES = {
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "uk": "GB",
    "united kingdom": "GB",
}


def _matches(city: City, qualifier: str) -> bool:
    q = qualifier.lower()
    return _COUNTRY_ALIASES.get(q) == city.country or q in {
        city.admin1_code.lower(),
        city.admin1_name.lower(),
        city.country.lower(),
        city.country_name.lower(),
    }


@lru_cache(maxsize=4096)
def geocode(location_text: str) -> tuple[float, float] | None:
    """(lat, lon) for 'City, ST' / 'City, State' / 'City, Country'; None when unknown."""
    parts = [re.sub(r"\b\d[\d-]*\b", "", p).strip() for p in location_text.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    candidates = _by_name().get(parts[0].lower(), [])
    matched_any = len(parts) == 1
    for qualifier in parts[1:]:
        narrowed = [c for c in candidates if _matches(c, qualifier)]
        # Unrecognized qualifiers (e.g. "NS": Canada's admin1 codes are numeric) are skipped,
        # but at least one qualifier must match so "Portland, XX" stays unknown.
        if narrowed:
            candidates, matched_any = narrowed, True
    if not candidates or not matched_any:
        return None
    best = candidates[0]  # lists are population-sorted
    return best.lat, best.lon


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))
