"""Resume-to-job match score, 0-100: TF-IDF cosine plus a marine skill keyword bonus."""

from __future__ import annotations

import re

from sklearn.feature_extraction.text import (  # type: ignore[import-untyped]
    TfidfVectorizer,
)
from sklearn.metrics.pairwise import linear_kernel  # type: ignore[import-untyped]

from marine_jobs.models import Job

_I = re.IGNORECASE
SKILLS = [
    re.compile(p, f)
    for p, f in [
        (r"\bGIS\b|\bArcGIS\b|\bQGIS\b", _I),
        (r"\bR\b(?! ?&)", 0),  # case-sensitive; skip "R&D"
        (r"\bpython\b", _I),
        (r"\bscuba\b|\bdive (?:certified|certification)\b|\bAAUS\b", _I),
        (r"\bboat (?:handling|operation|operator)\b|\bvessel operation", _I),
        (r"\bwater quality\b", _I),
        (r"\bspecies (?:identification|id)\b|\btaxonomic\b|\btaxonomy\b", _I),
        (r"\bfield (?:sampling|work|data collection)\b|\bfieldwork\b", _I),
        (r"\bdata analysis\b|\bstatistic", _I),
        (r"\bmatlab\b", _I),
        (r"\bCTD\b", 0),
        (r"\bplankton\b|\bzooplankton\b|\bphytoplankton\b", _I),
        (r"\bfisheries\b|\bfish survey", _I),
        (r"\bmicroscop", _I),
        (r"\bfirst aid\b|\bCPR\b", _I),
        (r"\bsnorkel", _I),
        (r"\bsediment\b", _I),
        (r"\bremote sensing\b", _I),
    ]
]
MAX_BONUS = 25.0
BONUS_PER_SKILL = 5.0
# Resume-vs-posting cosine rarely exceeds ~0.35, so map that range onto the base 0-75.
COSINE_CEILING = 0.35
BASE_WEIGHT = 75.0


def _skills(text: str) -> set[int]:
    return {i for i, rx in enumerate(SKILLS) if rx.search(text)}


def score(resume_text: str, jobs: list[Job]) -> list[float]:
    if not jobs:
        return []
    docs = [f"{j.title} {j.title} {j.description}" for j in jobs]
    try:
        matrix = TfidfVectorizer(
            stop_words="english", ngram_range=(1, 2), sublinear_tf=True
        ).fit_transform([resume_text, *docs])
    except ValueError:  # empty vocabulary: nothing but stop words or numbers
        return [0.0] * len(jobs)
    cosines = linear_kernel(matrix[0:1], matrix[1:]).ravel()
    resume_skills = _skills(resume_text)
    out = []
    for cos, doc in zip(cosines, docs, strict=True):
        base = min(float(cos) / COSINE_CEILING, 1.0) * BASE_WEIGHT
        bonus = min(len(resume_skills & _skills(doc)) * BONUS_PER_SKILL, MAX_BONUS)
        out.append(round(min(base + bonus, 100.0), 1))
    return out
