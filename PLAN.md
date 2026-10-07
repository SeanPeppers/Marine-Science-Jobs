# Marine Science Jobs — Plan

Personal job finder for full-time, entry-level (bachelor's, ~1 year experience) marine science jobs.
Hybrid design: GitHub Actions collects keyless sources daily; a local web app adds keyed sources,
reads the user's resume (PDF, in memory only), filters by distance, and ranks.

## Settled decisions

- Scope: all marine subfields, each job tagged with one `subfield`. Worldwide + remote. Also adjacent roles open to marine-science grads (GIS, environmental, water quality, ecology, wildlife, conservation), matched on title only.
- Exclude: jobs requiring MS/PhD, or 3+ years experience, or senior/manager/director titles, or with scam red flags (off-platform chat apps, personal email, check/fee/gift-card payment talk).
- Tags: `temp` (seasonal/temporary/contract/term), `us-citizen` (citizenship or clearance required; flag, not exclude).
- Rolling: no cycle. Mark `closed` when a source that ran successfully stops returning a job (per employer for ATS boards); no separate link check, since sources drop filled jobs themselves; delete 60 days after `last_seen` (the last day a source listed it).
- Dedup: fuzzy match on normalized company + title + location (rapidfuzz ratio >= 90), URL as tiebreaker.
- Cron: daily 12:00 UTC on GitHub Actions; commits `data/jobs.json` + README table.
- README table: Company | Role | Location | Subfield | Tags | Apply | Posted. No resume data, ever.
- Sources split: **keyless sources run in Actions**; **keyed sources run only in the local app with the user's own free keys** (`.env`, gitignored). Free tiers only. Respect robots.txt and ToS; follow the `api-integration-discipline` skill.
- Local app: FastAPI + one static HTML page with vanilla JS, `uv run marine-jobs` opens `http://localhost:8000`.
  - Location: city search box (offline GeoNames cities1000, CC-BY) + radius slider in miles + "include remote" and "remote only" toggles.
  - Resume: PDF only, parsed in memory, never written to disk. Non-PDF or unreadable PDF gives a plain error message.
  - Matching: TF-IDF cosine (scikit-learn) resume vs job title+description, plus bonus for matched marine skill keywords. Score 0-100.
  - Sort: group by posting day, newest day first; within a day, highest match first. Hide below a min-match threshold (slider).
  - "New since last visit" badge via localStorage.
  - Setup page to paste API keys (writes `.env`), with signup links.
- Repo public (`SeanPeppers/Marine-Science-Jobs`), MIT for code. The committed store keeps only a 300-char description snippet per job; full text is used locally only for keyed sources.
- Stack: Python 3.12, uv, httpx, feedparser, rapidfuzz, pyyaml, fastapi, uvicorn, pypdf, scikit-learn. ruff + mypy clean.

## Sources

| Source | Where | Notes |
|---|---|---|
| Conservation Job Board RSS `https://www.conservationjobboard.com/rss` | Actions | no auth; keyword filter client-side |
| ECO Magazine `https://ecomagazine.com/feed/?post_type=job` | Actions | small, high relevance |
| Canada Job Bank Atom `https://www.jobbank.gc.ca/jobsearch/feed/jobSearchRSSfeed?searchstring=...` | Actions | Crawl-delay 5 s |
| Greenhouse/Lever/Ashby/SmartRecruiters/Workday per employer, from `data/employers.yaml` | Actions | public JSON job-board APIs |
| USAJOBS `https://data.usajobs.gov/api/search` | Local | key + email User-Agent |
| Adzuna | Local | key; "Jobs by Adzuna" attribution; 250/day |
| Careerjet `https://search.api.careerjet.net/v4/query` | Local | key; needs user_ip + user_agent |
| Jooble `https://jooble.org/api/{key}` | Local | key |

Skipped (robots/ToS): Indeed, LinkedIn, SimplyHired, SEEK, NEOGOV. Pending permission: TAMU RWFM board, AFS job board.

## Module contract

```
src/marine_jobs/
  models.py            Job dataclass (shared; supervisor owns)
  sources/feeds.py     fetch(client: httpx.Client) -> list[Job]          (CJB, ECO, Job Bank)
  sources/ats.py       fetch(client: httpx.Client) -> list[Job]          (reads data/employers.yaml)
  sources/keyed.py     SOURCES: dict[str, KeyedSource]
                         KeyedSource: env_vars: list[str], signup_url: str,
                         fetch(client, env: Mapping[str, str]) -> list[Job]
  pipeline.py          classify(jobs) -> list[Job]   relevance filter, exclusions, subfield, tags
                       dedup(jobs) -> list[Job]
                       merge(store: list[Job], fresh: list[Job], today: str, sources_run: set[str]) -> list[Job]
                         sets first_seen/last_seen, closes jobs missing from a source that ran, drops >60 days
                       load_store(path) / save_store(path, jobs)
  readme.py            render(jobs) -> str  (writes table between markers in README.md)
  collect.py           CLI for Actions: keyless sources -> classify -> dedup -> merge -> data/jobs.json + README
  geo.py, match.py, app.py, static/index.html, __init__.py:main   local app
data/employers.yaml    - name, ats (greenhouse|lever|ashby|smartrecruiters|workday), slug, [workday host/site]
data/jobs.json         store (list of Job.to_dict())
```

Each fetch logs and skips a failing source rather than crashing the run.
