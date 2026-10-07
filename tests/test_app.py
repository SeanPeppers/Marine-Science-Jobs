from fastapi.testclient import TestClient

from marine_jobs import app as app_mod
from marine_jobs import geo, match
from marine_jobs.models import Job

RESUME = "B.S. Marine Biology. Field sampling, water quality monitoring, SCUBA certified, GIS, R."


def make_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1,
        xref,
    )
    return bytes(out)


def job(title: str, description: str, posted: str = "2026-09-30") -> Job:
    return Job(
        title,
        "Acme",
        "Tampa, FL",
        f"https://x.test/{title}",
        "test",
        posted,
        description,
    )


JOBS = [
    job(
        "Software Engineer",
        "Build React web apps and Kubernetes microservices in Java.",
    ),
    job(
        "Marine Field Technician",
        "Field sampling, water quality, SCUBA diving, GIS mapping in R.",
    ),
]


def test_txt_upload_rejected() -> None:
    resp = TestClient(app_mod.app, base_url="http://127.0.0.1:8000").post(
        "/api/jobs", files={"resume": ("resume.txt", b"hello", "text/plain")}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Please upload a PDF resume."


def test_pdf_upload_scores(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(app_mod, "all_jobs", lambda: list(JOBS))
    pdf = make_pdf("Marine biology field sampling water quality SCUBA GIS")
    resp = TestClient(app_mod.app, base_url="http://127.0.0.1:8000").post(
        "/api/jobs", files={"resume": ("cv.pdf", pdf, "application/pdf")}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scored"] is True
    assert body["jobs"][0]["title"] == "Marine Field Technician"


def test_score_ranks_marine_above_software() -> None:
    software, marine = match.score(RESUME, JOBS)
    assert marine > software
    assert all(0 <= s <= 100 for s in (software, marine))


def test_haversine() -> None:
    assert geo.haversine_miles(0, 0, 0, 0) == 0
    # Tampa to Miami is ~205 miles great-circle.
    assert 195 < geo.haversine_miles(27.9506, -82.4572, 25.7617, -80.1918) < 215


def test_remote_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    remote = job("Data Analyst", "Ocean data.")
    remote.remote = True
    monkeypatch.setattr(app_mod, "all_jobs", lambda: [*JOBS, remote])
    resp = TestClient(app_mod.app, base_url="http://127.0.0.1:8000").post(
        "/api/jobs", data={"remote_only": "true"}
    )
    assert resp.status_code == 200, resp.text
    assert [j["title"] for j in resp.json()["jobs"]] == ["Data Analyst"]
