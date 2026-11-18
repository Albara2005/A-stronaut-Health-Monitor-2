import json

from mqtt_bridge import MqttBridge, MAX_PAYLOAD_BYTES
from simulator import build_payload


def make_bridge(pipe):
    b = MqttBridge(pipe, host="localhost", prefix="orion")
    b.connected = True
    published = []
    b.client.publish = lambda topic, payload, qos=0, retain=False: published.append((topic, json.loads(payload), qos, retain))
    return b, published


def test_end_to_end_critical_via_mqtt_message(pipe):
    b, published = make_bridge(pipe)
    out = b.handle_message("orion/esp32-01/telemetry", json.dumps(build_payload("co2")).encode())
    assert out["decision"]["overall_status"] == "CRITICAL"
    # decision published back, retained, QoS1, compact (no evaluated_rules)
    topic, body, qos, retain = published[-1]
    assert topic == "orion/esp32-01/decision" and qos == 1 and retain is True
    assert body["overall_status"] == "CRITICAL" and "evaluated_rules" not in body
    assert "ECLSS-CO2-002" in {r["rule_id"] for r in body["matched_rules"]}
    # persisted
    assert pipe.storage.history("esp32-01")[0]["overall_status"] == "CRITICAL"


def test_nominal_then_repeat_is_deduplicated(pipe):
    b, _ = make_bridge(pipe)
    msg = json.dumps(build_payload("nominal")).encode()
    first = b.handle_message("orion/d1/telemetry", msg)
    second = b.handle_message("orion/d1/telemetry", msg)
    assert first["stored"] is True and second["stored"] is False
    assert len(pipe.storage.history("d1")) == 1
    b.handle_message("orion/d1/telemetry", json.dumps(build_payload("spo2")).encode())
    assert len(pipe.storage.history("d1")) == 2


def test_bad_messages_never_raise(pipe):
    b, _ = make_bridge(pipe)
    assert b.handle_message("orion/d1/telemetry", b"{not json") is None
    assert b.handle_message("orion/d1/telemetry", b"\xff\xfe") is None
    assert b.handle_message("orion/d1/telemetry", b"[1,2]") is None
    assert b.handle_message("orion/d1/telemetry", b"x" * (MAX_PAYLOAD_BYTES + 1)) is None
    assert b.handle_message("other/d1/telemetry", b"{}") is None
    assert b.handle_message("orion/d1/decision", b"{}") is None
    assert b.handle_message("orion/a/b/telemetry", b"{}") is None
    assert pipe.storage.history() == []


def test_topic_is_authoritative_for_device_id(pipe):
    b, _ = make_bridge(pipe)
    payload = dict(build_payload("nominal"), device_id="spoofed")
    out = b.handle_message("orion/real-01/telemetry", json.dumps(payload).encode())
    assert out["device_id"] == "real-01"


def test_publish_skipped_when_disconnected(pipe):
    b, published = make_bridge(pipe)
    b.connected = False
    b.handle_message("orion/d1/telemetry", json.dumps(build_payload("nominal")).encode())
    assert published == []


def test_custom_multi_level_prefix(pipe):
    b = MqttBridge(pipe, prefix="mission/orion")
    assert b.telemetry_filter == "mission/orion/+/telemetry"
    out = b.handle_message("mission/orion/d9/telemetry", json.dumps(build_payload("nominal")).encode())
    assert out["device_id"] == "d9"
