import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

import main
from core.database import DuckDBClient


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    records = [
        {
            "agency_id": "alpha.example",
            "agency_name": "Alpha",
            "industry": "Technology",
            "qualification_score": 90,
            "updated_at": "2026-01-03T00:00:00Z",
            "decision_makers": [{"full_name": "A. Person"}],
        },
        {
            "agency_id": "beta.example",
            "agency_name": "Beta",
            "industries": ["Technology", "Consulting"],
            "score_breakdown": {"composite_score": 75},
            "updated_at": "2026-01-02T00:00:00Z",
        },
        {
            "agency_id": "gamma.example",
            "agency_name": "Gamma",
            "industry": "Retail",
            "qualification_status": "pending_review",
        },
    ]
    prospects_file = tmp_path / "prospects.jsonl"
    prospects_file.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "PROSPECTS_PATH", prospects_file)
    monkeypatch.setattr(main, "ADMIN_API_KEY", "admin-test-key")
    monkeypatch.setattr(main, "READ_ONLY_API_KEYS", ("read-test-key",))
    monkeypatch.setattr(main, "API_RATE_LIMIT_PER_MINUTE", 60)
    with main._rate_limit_lock:
        main._rate_limit_windows.clear()
    return TestClient(main.app), records


def test_list_prospects_filters_and_paginates(api_client):
    client, _ = api_client
    response = client.get(
        "/api/v1/prospects",
        params={
            "limit": 1,
            "offset": 1,
            "qualification_score_min": 70,
            "industry": "technology",
            "updated_since": "2026-01-01T00:00:00Z",
        },
        headers={"X-API-Key": "read-test-key"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "prospects": [
            {
                "agency_id": "beta.example",
                "agency_name": "Beta",
                "industries": ["Technology", "Consulting"],
                "score_breakdown": {"composite_score": 75},
                "updated_at": "2026-01-02T00:00:00Z",
            }
        ],
        "total_count": 2,
        "has_more": False,
        "page": 2,
    }


def test_prospect_detail_returns_full_record(api_client):
    client, records = api_client
    response = client.get(
        "/api/v1/prospects/alpha.example",
        headers={"X-API-Key": "read-test-key"},
    )

    assert response.status_code == 200
    assert response.json() == records[0]
    assert client.get(
        "/api/v1/prospects/unknown.example",
        headers={"X-API-Key": "read-test-key"},
    ).status_code == 404


def test_exports_stream_csv_and_jsonl(api_client):
    client, records = api_client
    headers = {"X-API-Key": "read-test-key"}

    csv_response = client.get(
        "/api/v1/prospects/export",
        params={"format": "csv", "limit": 1},
        headers=headers,
    )
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    csv_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert len(csv_rows) == 1
    assert csv_rows[0]["agency_id"] == records[0]["agency_id"]

    jsonl_response = client.get(
        "/api/v1/prospects/export",
        params={"format": "jsonl", "offset": 1, "limit": 1},
        headers=headers,
    )
    assert jsonl_response.status_code == 200
    assert json.loads(jsonl_response.text) == records[1]


def test_read_only_key_scope_and_rate_limit_headers(api_client, monkeypatch):
    client, _ = api_client
    read_response = client.get(
        "/api/v1/prospects",
        headers={"X-API-Key": "read-test-key"},
    )
    assert read_response.status_code == 200
    assert read_response.headers["x-ratelimit-limit"] == "60"
    assert read_response.headers["x-ratelimit-remaining"] == "59"

    forbidden = client.post(
        "/api/v1/trigger-pipeline",
        headers={"X-API-Key": "read-test-key"},
        json={},
    )
    assert forbidden.status_code == 403
    admin = client.get(
        "/api/v1/prospects",
        headers={"X-API-Key": "admin-test-key"},
    )
    assert admin.status_code == 200
    assert admin.headers["x-ratelimit-limit"] == "60"
    admin_trigger = client.post(
        "/api/v1/trigger-pipeline",
        headers={"X-API-Key": "admin-test-key"},
        json={},
    )
    assert admin_trigger.status_code == 503

    monkeypatch.setattr(main, "API_RATE_LIMIT_PER_MINUTE", 1)
    with main._rate_limit_lock:
        main._rate_limit_windows.clear()
    assert client.get(
        "/api/v1/prospects",
        headers={"X-API-Key": "read-test-key"},
    ).status_code == 200
    limited = client.get(
        "/api/v1/prospects",
        headers={"X-API-Key": "read-test-key"},
    )
    assert limited.status_code == 429
    assert limited.headers["x-ratelimit-limit"] == "1"
    assert limited.headers["x-ratelimit-remaining"] == "0"
    assert "retry-after" in limited.headers


def test_openapi_exposes_prospect_delivery_and_compact_validation(api_client):
    client, _ = api_client
    schema = client.get("/openapi.json").json()

    assert "/api/v1/prospects/export" in schema["paths"]
    assert "/api/v1/prospects/{prospect_id}" in schema["paths"]
    assert "/api/v1/dispatch/process" not in schema["paths"]
    assert "429" in schema["paths"]["/api/v1/prospects"]["get"]["responses"]
    invalid = client.get(
        "/api/v1/prospects",
        params={"limit": 0},
        headers={"X-API-Key": "read-test-key"},
    )
    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "Invalid request."}


def test_health_check_uses_existing_duckdb_connection(tmp_path, monkeypatch):
    client = TestClient(main.app)
    db_client = DuckDBClient(db_path=tmp_path / "health.duckdb")
    monkeypatch.setattr(main, "db_client", db_client)
    try:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "healthy",
            "service": "ADL-SignalForge",
            "storage_accessible": True,
        }
    finally:
        db_client.close()
