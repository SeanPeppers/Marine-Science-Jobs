from marine_jobs.models import Job
from marine_jobs.pipeline import classify, dedup, merge


def job(title: str = "Marine Technician", desc: str = "", **kw: str) -> Job:
    base = {
        "company": "NOAA Fisheries",
        "location": "Seattle, WA",
        "url": f"https://x.test/{title}/{desc}",
        "source": "greenhouse",
        "posted": "2026-09-01",
    } | kw
    return Job(title=title, description=desc, **base)  # type: ignore[arg-type]


def titles(jobs: list[Job]) -> set[str]:
    return {j.title for j in classify(jobs)}


def test_exclusions() -> None:
    jobs = [
        job("Marine A", "Master's degree required."),
        job("Marine B", "Bachelor's required; Master's preferred."),
        job("Marine C", "Requires 3+ years of field experience."),
        job("Marine D", "1-2 years of experience with boats."),
        job("Marine E", "At least four years of relevant experience."),
        job("Senior Marine Scientist"),
    ]
    assert titles(jobs) == {"Marine B", "Marine D"}


def test_tags_and_relevance() -> None:
    temp, citizen = classify(
        [
            job("Seasonal Fisheries Technician", "6-month position."),
            job("Ocean Data Analyst", "Must be a U.S. citizen."),
            job(
                "Satellite Software Engineer",
                "Build imaging pipelines.",
                company="Planet Labs",
            ),
        ]
    )
    assert temp.tags == ["temp"] and temp.subfield == "fisheries"
    assert citizen.tags == ["us-citizen"]


def test_fuzzy_dedup() -> None:
    a = job("Marine Biologist I", "short", url="https://a")
    b = job(
        "Marine Biologist 1",
        "much longer text",
        company="NOAA fisheries",
        url="https://b",
    )
    b.posted = "2026-08-30"
    (only,) = dedup([a, b])
    assert only.description == "much longer text" and only.posted == "2026-08-30"
    assert len(dedup([a, job("Marine Biologist II", "x", url="https://c")])) == 2


def test_merge() -> None:
    kept = job("Kept", first_seen="2026-09-20")
    gone = job("Gone", first_seen="2026-09-20")
    stale = job(
        "Stale", first_seen="2026-06-01", last_seen="2026-07-01", source="lever"
    )
    out = {
        j.title: j
        for j in merge([kept, gone, stale], [job("Kept")], "2026-10-01", {"greenhouse"})
    }
    assert set(out) == {"Kept", "Gone"}
    assert (
        out["Kept"].first_seen == "2026-09-20" and out["Kept"].last_seen == "2026-10-01"
    )
    assert out["Gone"].closed and not out["Kept"].closed
