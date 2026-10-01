"""Shared job record. Every source, the pipeline, and the app exchange this type."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Job:
    title: str
    company: str
    location: str  # raw location text from the source; geocoded only in the local app
    url: str
    source: str  # collector name, e.g. "greenhouse", "usajobs"
    posted: str  # ISO date YYYY-MM-DD; first_seen date when the source gives none
    description: str = ""  # plain text, capped at DESCRIPTION_CAP chars
    remote: bool = False
    subfield: str | None = None  # set by pipeline.classify
    tags: list[str] = field(default_factory=list)  # e.g. "temp", "us-citizen"
    first_seen: str = ""
    last_seen: str = ""
    closed: bool = False

    @property
    def id(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"id": self.id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        return cls(**{k: v for k, v in data.items() if k != "id"})


DESCRIPTION_CAP = 4000
