import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

from orion_app import create_app  # noqa: E402
from simulator import build_payload  # noqa: E402


@pytest.fixture
def client():
    with TestClient(create_app(db_path=":memory:", enable_mqtt=False)) as c:
        yield c


def post(client, device="esp32-01", **data):
    return client.post("/api/embedded/telemetry", json={"device_id": device, "data": data})


def test_health_and_status(client):
    assert client.get("/health").json() == {"ok": True, "mqtt_enabled": False, "mqtt_connected": False}
    s = client.get("/api/expert/status").json()
    assert s["status"] == "ONLINE" and s["mqtt_connected"] is False


def test_analyze_without_telemetry_is_409(client):
    assert client.post("/api/expert/analyze").status_code == 409


def test_http_ingest_end_to_end(client):
    r = post(client, **build_payload("co2")).json()
    assert r["accepted"] and r["overall_status"] == "CRITICAL" and r["stored"] is True
    assert client.get("/api/expert/latest").json()["overall_status"] == "CRITICAL"
    items = client.get("/api/expert/decisions").json()["items"]
    assert items[0]["matched_rule_ids"][0] == "ECLSS-CO2-002"
    assert client.get("/api/expert/devices").json() == {"devices": ["esp32-01"]}
    assert client.post("/api/expert/analyze").json()["overall_status"] == "CRITICAL"


def test_validation_errors(client):
    bad_ts = client.post("/api/embedded/telemetry",
                         json={"device_id": "d1", "timestamp": "someday", "data": {}})
    assert bad_ts.status_code == 422
    assert client.post("/api/embedded/telemetry",
                       json={"device_id": "bad id!", "data": {}}).status_code == 422


def test_per_device_filtering(client):
    post(client, "a", **build_payload("co2"))
    post(client, "b", **build_payload("nominal"))
    assert client.get("/api/expert/latest", params={"device_id": "b"}).json()["overall_status"] == "NOMINAL"
    assert len(client.get("/api/expert/decisions", params={"device_id": "a"}).json()["items"]) == 1
    assert client.get("/api/expert/latest", params={"device_id": "zzz"}).status_code == 404


# ---- v1.3: overview, telemetry, watchdog, API key, frontend --------------------

def test_overview_shape_and_fresh_decision(client):
    empty = client.get("/api/expert/overview").json()
    assert empty["decision"] is None and empty["devices"] == [] and empty["device_id"] is None
    post(client, "a", **build_payload("co2"))
    o = client.get("/api/expert/overview").json()
    assert o["device_id"] == "a" and o["devices"] == ["a"]
    assert o["decision"]["overall_status"] == "CRITICAL"
    assert o["decision"]["matched_rules"][0]["recommendation"]
    assert o["telemetry"]["eclss"]["co2_ppm"] == 1300
    assert o["history"][0]["overall_status"] == "CRITICAL" and o["health"]["ok"] is True


def test_overview_defaults_to_device_that_sent_last_and_filters(client):
    post(client, "a", **build_payload("co2"))
    post(client, "b", **build_payload("nominal"))
    post(client, "a", **build_payload("co2"))          # 'a' sends again -> it is the most recent
    assert client.get("/api/expert/overview").json()["device_id"] == "a"
    assert client.get("/api/expert/overview", params={"device_id": "b"}).json()["decision"]["overall_status"] == "NOMINAL"
    unknown = client.get("/api/expert/overview", params={"device_id": "zzz"}).json()
    assert unknown["decision"] is None


def test_telemetry_endpoint(client):
    assert client.get("/api/expert/telemetry").status_code == 404
    post(client, "a", co2_ppm=5)
    assert client.get("/api/expert/telemetry").json()["eclss"]["co2_ppm"] == 5


def test_reanalyze_uses_most_recent_device(client):
    post(client, "a", **build_payload("co2"))
    post(client, "b", **build_payload("nominal"))
    assert client.post("/api/expert/analyze").json()["overall_status"] == "NOMINAL"


def test_watchdog_detects_silent_device(client):
    """No new telemetry arrives; staleness must still be detected by the periodic re-analysis."""
    from datetime import datetime, timedelta, timezone
    pipe = client.app.state.pipeline
    post(client, "a", **build_payload("nominal"))
    pipe._latest["a"]["received_at"] = pipe._latest["a"]["timestamp"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert pipe.reanalyze_all() == 1
    items = client.get("/api/expert/decisions", params={"device_id": "a"}).json()["items"]
    assert items[0]["overall_status"] == "WARNING" and "DATA-TIME-002" in items[0]["matched_rule_ids"]
    o = client.get("/api/expert/overview").json()
    assert o["decision"]["data_quality"]["stale"] is True


def test_api_key_protects_ingest_only():
    with TestClient(create_app(db_path=":memory:", enable_mqtt=False, api_key="s3cret")) as c:
        body = {"device_id": "d", "data": {}}
        assert c.post("/api/embedded/telemetry", json=body).status_code == 401
        assert c.post("/api/embedded/telemetry", json=body, headers={"X-API-Key": "nope"}).status_code == 401
        assert c.post("/api/embedded/telemetry", json=body, headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get("/api/expert/overview").status_code == 200      # read endpoints stay open


def test_frontend_is_served_and_api_routes_not_shadowed(client):
    page = client.get("/")
    assert page.status_code == 200 and "ORION-IV" in page.text and "/api/expert/overview" in page.text
    assert client.get("/health").json()["ok"] is True
    assert client.get("/api/expert/status").json()["status"] == "ONLINE"
