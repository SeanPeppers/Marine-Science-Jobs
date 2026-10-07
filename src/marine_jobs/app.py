"""Local web app: resume match, distance filter, keyed sources. Bind to localhost only."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from marine_jobs import geo, match, pipeline
from marine_jobs.models import Job
from marine_jobs.sources import keyed

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
STORE = REPO / "data" / "jobs.json"
ENV_FILE = REPO / ".env"
STATIC = Path(__file__).parent / "static"
KEYED_CACHE = geo.CACHE_DIR / "keyed.json"
KEYED_TTL_S = 12 * 3600
MAX_PDF_BYTES = 10 * 1024 * 1024
GEONAMES_CREDIT = "Locations: GeoNames (geonames.org), CC-BY 4.0"
NOT_PDF = "Please upload a PDF resume."
NO_TEXT = (
    "Couldn't read text from that PDF — is it a scanned image? "
    "Try exporting it from Word or Google Docs as PDF."
)
ENCRYPTED = "That PDF is password-protected. Please upload an unlocked copy."
ENV_VALUE_RE = re.compile(r"^[^=\r\n]{1,300}$")

app = FastAPI(title="Marine Science Jobs", docs_url=None, redoc_url=None)
LOCAL_HOSTS = {"127.0.0.1:8000", "localhost:8000"}


@app.middleware("http")
async def local_only(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Block DNS rebinding (Host) and cross-site POSTs (Origin) from other web pages."""
    origin = request.headers.get("origin")
    if request.headers.get("host") not in LOCAL_HOSTS or (
        origin and origin.split("://", 1)[-1] not in LOCAL_HOSTS
    ):
        return PlainTextResponse("Forbidden", status_code=403)
    return await call_next(request)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/cities")
def cities(q: str = "") -> list[dict[str, Any]]:
    return [
        {"label": c.label, "lat": c.lat, "lon": c.lon}
        for c in geo.search_cities(q[:100], 10)
    ]


def resume_text(upload: UploadFile) -> str:
    """Extract text in memory; the file is never written to disk or logged."""
    name = (upload.filename or "").lower()
    if not name.endswith(".pdf") and upload.content_type != "application/pdf":
        raise HTTPException(400, NOT_PDF)
    data = upload.file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(
            400, "That PDF is over 10 MB. Please upload a smaller file."
        )
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, NOT_PDF)
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise HTTPException(400, ENCRYPTED)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(400, NOT_PDF) from exc
    if not text.strip():
        raise HTTPException(400, NO_TEXT)
    return text


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#"):
                env[key.strip()] = value.strip()
    return env


def keyed_jobs() -> list[Job]:
    """Keyed-source jobs, cached 12 h so free-tier quotas last."""
    try:
        cached = json.loads(KEYED_CACHE.read_text())
        if time.time() - cached["fetched"] < KEYED_TTL_S:
            return [Job.from_dict(d) for d in cached["jobs"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    env = dict(os.environ) | read_env()
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        jobs = pipeline.classify(keyed.run_all(client, env))
    KEYED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    KEYED_CACHE.write_text(
        json.dumps({"fetched": time.time(), "jobs": [j.to_dict() for j in jobs]})
    )
    return jobs


def all_jobs() -> list[Job]:
    store = pipeline.load_store(STORE) if STORE.exists() else []
    return pipeline.dedup([j for j in store if not j.closed] + keyed_jobs())


@app.post("/api/jobs")
def jobs(
    resume: UploadFile | None = None,
    lat: Annotated[float | None, Form(ge=-90, le=90)] = None,
    lon: Annotated[float | None, Form(ge=-180, le=180)] = None,
    radius_miles: Annotated[float, Form(ge=5, le=500)] = 100,
    include_remote: Annotated[bool, Form()] = True,
    remote_only: Annotated[bool, Form()] = False,
    min_score: Annotated[float, Form(ge=0, le=100)] = 0,
) -> dict[str, Any]:
    text = resume_text(resume) if resume is not None and resume.filename else None
    rows: list[tuple[Job, float | None, bool]] = []  # job, distance, location unknown
    for job in all_jobs():
        if job.remote:
            if include_remote:
                rows.append((job, None, False))
            continue
        if remote_only:
            continue
        if lat is None or lon is None:
            rows.append((job, None, False))
            continue
        # Multi-site postings list "City, ST; City, ST"; the nearest site counts.
        points = [p for part in job.location.split(";") if (p := geo.geocode(part))]
        if not points:
            rows.append((job, None, True))
            continue
        dist = min(geo.haversine_miles(lat, lon, *p) for p in points)
        if dist <= radius_miles:
            rows.append((job, round(dist), False))
    scores: list[float | None] = (
        list(match.score(text, [r[0] for r in rows])) if text else [None] * len(rows)
    )
    out = [
        job.to_dict() | {"score": s, "distance_miles": d, "location_unknown": unknown}
        for (job, d, unknown), s in zip(rows, scores, strict=True)
        if s is None or s >= min_score
    ]
    out.sort(key=lambda j: (j["posted"], j["score"] or 0), reverse=True)
    sources = {j["source"] for j in out}
    credits = [
        keyed.ATTRIBUTIONS[s] for s in sorted(sources) if s in keyed.ATTRIBUTIONS
    ]
    return {
        "jobs": out,
        "attributions": [*credits, GEONAMES_CREDIT],
        "scored": text is not None,
    }


@app.post("/api/refresh")
def refresh() -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "message": f"git pull failed: {exc}"}
    return {"ok": proc.returncode == 0, "message": (proc.stdout + proc.stderr).strip()}


@app.get("/api/keys")
def get_keys() -> list[dict[str, Any]]:
    env = read_env()
    return [
        {
            "source": name,
            "signup_url": src.signup_url,
            "env_vars": [
                {"name": v, "set": bool(env.get(v) or os.environ.get(v))}
                for v in src.env_vars
            ],
        }
        for name, src in keyed.SOURCES.items()
    ]


@app.post("/api/keys")
def set_keys(values: dict[str, str]) -> list[dict[str, Any]]:
    allowed = {v for src in keyed.SOURCES.values() for v in src.env_vars}
    env = read_env()
    for key, value in values.items():
        value = value.strip()
        if key not in allowed:
            raise HTTPException(400, f"Unknown setting: {key[:50]}")
        if value and not ENV_VALUE_RE.match(value):
            raise HTTPException(400, f"{key}: value must be one line without '='.")
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    ENV_FILE.chmod(0o600)
    KEYED_CACHE.unlink(missing_ok=True)  # new keys → refetch
    return get_keys()
