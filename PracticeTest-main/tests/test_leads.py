import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import consume_oauth_state, create_app, store_oauth_state, init_db

SECRET = "test-signing-secret"
ADMIN_USER = "test-admin"
ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture()
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "leads.db"),
        signing_secret=SECRET,
        admin_username=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
        google_client_secrets_file=str(tmp_path / "missing-google-oauth.json"),
        spreadsheet_id="",
    )
    app.state.sheets_sync = lambda lead: {"status": "synced"}
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides):
    data = {
        "id": "partner-001",
        "name": "Avery Chen",
        "email": "avery@example.com",
        "company": "Northstar Labs",
        "source": "partner-site",
    }
    data.update(overrides)
    return data


def post_signed(client, data, secret=SECRET):
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/api/leads", content=body, headers={
        "Content-Type": "application/json",
        "X-Lead-Signature": signature,
    })


def test_invalid_signature_is_rejected_and_does_not_save(client):
    response = post_signed(client, payload(), secret="wrong-secret")
    assert response.status_code == 401
    assert client.get("/api/leads", auth=(ADMIN_USER, ADMIN_PASSWORD)).json() == []


def test_duplicate_submission_does_not_create_another_record(client):
    first = post_signed(client, payload())
    second = post_signed(client, payload())
    assert first.status_code == 201 and first.json()["action"] == "created"
    assert second.status_code == 200 and second.json()["action"] == "duplicate"
    rows = client.get("/api/leads", auth=(ADMIN_USER, ADMIN_PASSWORD)).json()
    assert len(rows) == 1
    assert rows[0]["external_id"] == "partner-001"


def test_existing_email_updates_existing_record(client):
    first = post_signed(client, payload())
    second = post_signed(client, payload(
        id="partner-999", name="Avery Chen Updated", email="AVERY@example.com",
        company="Northstar Group", source="campaign",
    ))
    assert first.status_code == 201
    assert second.status_code == 200 and second.json()["action"] == "updated"
    rows = client.get("/api/leads", auth=(ADMIN_USER, ADMIN_PASSWORD)).json()
    assert len(rows) == 1
    assert rows[0]["name"] == "Avery Chen Updated"
    assert rows[0]["company"] == "Northstar Group"
    assert rows[0]["external_id"] == "partner-001"


def test_invalid_payload_is_rejected(client):
    assert post_signed(client, payload(email="not-an-email")).status_code == 422


def test_stage_update_is_saved_and_synced(client):
    post_signed(client, payload())
    response = client.patch("/api/leads/partner-001/stage", json={"stage": "Qualified"},
                            auth=(ADMIN_USER, ADMIN_PASSWORD))
    assert response.status_code == 200
    assert response.json()["lead"]["stage"] == "Qualified"
    assert response.json()["lead"]["sheets_sync_status"] == "synced"


def test_admin_lead_list_requires_authentication(client):
    assert client.get("/api/leads").status_code == 401


def test_oauth_state_is_single_use(tmp_path):
    path = str(tmp_path / "oauth-state.db")
    init_db(path)
    store_oauth_state(path, "one-time-state")
    assert consume_oauth_state(path, "one-time-state") is True
    assert consume_oauth_state(path, "one-time-state") is False
    assert consume_oauth_state(path, "unknown") is False


def test_conflicting_id_and_email_are_rejected_without_merging(client):
    post_signed(client, payload(id="first", email="first@example.com"))
    post_signed(client, payload(id="second", email="second@example.com"))
    response = post_signed(client, payload(id="first", email="second@example.com", name="Do not merge"))
    assert response.status_code == 409
    rows = client.get("/api/leads", auth=(ADMIN_USER, ADMIN_PASSWORD)).json()
    assert len(rows) == 2
    assert all(row["name"] != "Do not merge" for row in rows)
