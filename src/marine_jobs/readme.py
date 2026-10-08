"""Render the job table into README.md between markers."""

from __future__ import annotations

import re
from pathlib import Path

from marine_jobs.models import Job
from marine_jobs.pipeline import sort_newest

START, END = "<!-- JOBS:START -->", "<!-- JOBS:END -->"
TAG_EMOJI = {"us-citizen": "🇺🇸", "temp": "⏳", "degree-fit": "🎓"}
HEADER = "| Company | Role | Location | Subfield | Tags | Apply | Posted |\n|---|---|---|---|---|---|---|"


def _cell(value: str) -> str:
    return " ".join(value.split()).replace("|", r"\|")


def _apply(job: Job) -> str:
    if job.closed:
        return "🔒"
    url = job.url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
    return f"[Apply]({url})"


def render(jobs: list[Job]) -> str:
    rows = [
        "| "
        + " | ".join(
            [
                _cell(j.company),
                _cell(j.title),
                _cell("Remote" if j.remote and not j.location else j.location),
                _cell(j.subfield or "other"),
                " ".join(TAG_EMOJI.get(t, t) for t in j.tags),
                _apply(j),
                j.posted,
            ]
        )
        + " |"
        for j in sort_newest(jobs)
    ]
    return "\n".join([HEADER, *rows])


def write_readme(path: Path, jobs: list[Job]) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + ".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"{path} lacks {START} / {END} markers")
    block = f"{START}\n{render(jobs)}\n{END}"
    path.write_text(pattern.sub(lambda _: block, text), encoding="utf-8")
