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


def test_scam_filter() -> None:
    jobs = [
        job("Marine A", "Interview on Telegram with our hiring manager."),
        job("Marine B", "Send your resume to oceanjobs123@gmail.com."),
        job("Marine C", "We will mail you a check to deposit for equipment."),
        job("Marine D", "A $50 training fee is required."),
        job("Marine E", "No experience required. Apply at noaa.gov."),
    ]
    assert titles(jobs) == {"Marine E"}


def test_adjacent_titles() -> None:
    jobs = [
        job("GIS Analyst", company="Acme"),
        job("Water Quality Technician", company="Acme"),
        job("Accountant", "Supports our conservation mission.", company="Acme"),
        job("Archaeological Field Technician", company="Acme"),
        job("Stormwater Engineer", company="Acme"),
        job("Assistant Professor, Wildlife Biologist", company="Acme"),
        job("Conservation Science Volunteer", company="Acme"),
    ]
    assert titles(jobs) == {"GIS Analyst", "Water Quality Technician"}


def test_adjacent_biology_is_field_not_medical() -> None:
    jobs = [
        job("Wildlife Biologist", company="Acme"),
        job("Biologiste Médical (F/H)", company="Acme"),
        job("Biologics HPLC Scientist", company="Acme"),
    ]
    assert titles(jobs) == {"Wildlife Biologist"}


def test_location_rules() -> None:
    jobs = [
        job("Marine A", location="Tampa, FL"),
        job("Marine B", location="Remote - USA"),
        job("Marine C", location="SeaWorld Orlando"),
        job("Marine D", location="Merrillville, IN, US"),
        job("Marine E", location="Gurugram, HR, IN"),
        job("Marine F", location="Milan, IT"),
        job("Marine F2", location="Bangalore, IN"),
        job("Marine G", location="Melbourne, Australia"),
        job("Marine H", location="Mississauga, ON, CA"),
        job("Marine I", location="Seattle, WA; Vancouver, BC"),
        job("Marine J", location="Remote, Egypt"),
        job(
            "Marine K",
            "Salary £18,000 - £22,000 per annum.",
            location="Remote, England",
        ),
        job("Marine L", "Pay: $30 to $35 per hour.", location="Remote, Canada"),
    ]
    assert titles(jobs) == {
        "Marine A",
        "Marine B",
        "Marine C",
        "Marine D",
        "Marine I",
        "Marine J",
        "Marine L",
    }


def test_aggregator_noise() -> None:
    jobs = [
        job("Inland Marine Desk Adjuster", company="Acme"),
        job("Ocean Export Agent", company="Acme"),
        job("Molecular Biologist - Fully Remote", company="Acme"),
        job(
            "Operations Coordinator",
            "Ocean freight team.",
            company="Acme",
            source="himalayas",
        ),
        job("Jr. Geospatial Analyst", company="Acme", source="himalayas"),
    ]
    assert titles(jobs) == {"Jr. Geospatial Analyst"}


def test_degree_fit_tag() -> None:
    fit, related, other = classify(
        [
            job("Marine A", "Bachelor's degree in marine biology or a related field."),
            job("Marine B", "B.S. in environmental science, ecology, or related."),
            job("Marine C", "Degree in accounting preferred."),
        ]
    )
    assert "degree-fit" in fit.tags and "degree-fit" in related.tags
    assert "degree-fit" not in other.tags
