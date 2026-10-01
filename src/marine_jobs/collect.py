"""Actions CLI: keyless sources -> classify -> dedup -> merge -> data/jobs.json + README."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from marine_jobs import pipeline, readme
from marine_jobs.sources import ats, feeds

STORE = Path("data/jobs.json")
README = Path("README.md")
# The store is public: keep only a snippet of each source's text, the link carries the rest.
PUBLIC_SNIPPET = 300
USER_AGENT = "marine-jobs/0.1 (+https://github.com/SeanPeppers/Marine-Science-Jobs)"

log = logging.getLogger("marine_jobs.collect")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    today = datetime.now(UTC).date().isoformat()
    with httpx.Client(
        timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        feed_jobs = feeds.fetch(client)
        ats_jobs = ats.fetch(client)
    log.info("feeds: %d fetched, ats: %d fetched", len(feed_jobs), len(ats_jobs))
    raw = feed_jobs + ats_jobs
    # A failed source returns nothing, so it is absent here and its stored jobs stay open.
    # ATS sources are tracked per employer so one failing board can't close another's jobs.
    sources_run = {j.source for j in feed_jobs} | {
        f"{j.source}:{j.company}" for j in ats_jobs
    }
    kept = pipeline.classify(raw)
    log.info("classify: %d -> %d", len(raw), len(kept))
    unique = pipeline.dedup(kept)
    log.info("dedup: %d -> %d", len(kept), len(unique))
    store = pipeline.load_store(STORE)
    merged = pipeline.merge(store, unique, today, sources_run)
    log.info(
        "merge: store %d + fresh %d -> %d (%d closed)",
        len(store),
        len(unique),
        len(merged),
        sum(j.closed for j in merged),
    )
    public = [replace(j, description=j.description[:PUBLIC_SNIPPET]) for j in merged]
    pipeline.save_store(STORE, public)
    readme.write_readme(README, merged)


if __name__ == "__main__":
    main()
