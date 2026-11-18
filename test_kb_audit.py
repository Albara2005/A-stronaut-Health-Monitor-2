"""Audit the knowledge base itself: every rule must fire exactly at its threshold, and every
field a rule reads must be reachable from what a device can actually send."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from expert_system import ExpertSystem, get_path
from normalizer import FLAT_MAP, NESTED_KEYS, normalize

KB = json.loads((Path(__file__).resolve().parent.parent / "knowledge_base.json").read_text(encoding="utf-8"))
ENGINE = ExpertSystem(KB)
NOW = datetime.now(timezone.utc)


def healthy():
    return normalize("t1", {
        "heart_rate_bpm": 75, "spo2_percent": 98, "co2_ppm": 600, "scrubber_status": "ACTIVE",
        "cabin_pressure_kpa": 101, "ppo2_kpa": 21, "radiation_level": 0.1,
        "co2_sensor_status": "ONLINE", "pressure_sensor_status": "ONLINE",
        "radiation_sensor_status": "ONLINE", "data_valid": True}, NOW)


def set_path(d, path, value):
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def fired(state):
    return {r["rule_id"] for r in ENGINE.analyze(state, now=NOW)["matched_rules"]}


def test_healthy_baseline_is_nominal():
    assert ENGINE.analyze(healthy(), now=NOW)["overall_status"] == "NOMINAL"


NUMERIC = [(r["rule_id"], c) for r in KB["rules"] for g in ("all", "any") for c in r.get(g, [])
           if c["operator"] in (">", "<", ">=", "<=") and isinstance(c["value"], (int, float))]


@pytest.mark.parametrize("rule_id,cond", NUMERIC, ids=[f"{r}:{c['field']}{c['operator']}{c['value']}" for r, c in NUMERIC])
def test_numeric_threshold_boundaries(rule_id, cond):
    """Strict operators must NOT fire exactly at the threshold, and must fire just past it."""
    th, op = cond["value"], cond["operator"]
    inside, outside = {">": (th + 0.01, th), "<": (th - 0.01, th),
                       ">=": (th, th - 0.01), "<=": (th, th + 0.01)}[op]
    # make the rule's OTHER conditions true so only this threshold decides
    rule = next(r for r in KB["rules"] if r["rule_id"] == rule_id)
    state = healthy()
    for other in rule.get("all", []):
        if other is not cond and other["operator"] == "==":
            set_path(state, other["field"], other["value"])
    set_path(state, cond["field"], inside)
    assert rule_id in fired(state), f"{rule_id} should fire at {inside}"
    set_path(state, cond["field"], outside)
    assert rule_id not in fired(state), f"{rule_id} must not fire at {outside}"


def test_co2_critical_requires_scrubber_inactive():
    s = healthy(); set_path(s, "eclss.co2_ppm", 2000)
    assert "ECLSS-CO2-002" not in fired(s)
    set_path(s, "eclss.scrubber_status", "INACTIVE")
    assert "ECLSS-CO2-002" in fired(s)


@pytest.mark.parametrize("sensor", ["co2_sensor", "pressure_sensor", "radiation_sensor"])
def test_each_offline_sensor_triggers_sensor_rule(sensor):
    s = healthy(); set_path(s, f"devices.{sensor}.status", "OFFLINE")
    assert "SENSOR-001" in fired(s)


def test_data_invalid_triggers_data_rule():
    s = healthy(); set_path(s, "data_quality.valid", False)
    assert "DATA-001" in fired(s)


def all_rule_fields():
    return sorted({c["field"] for r in KB["rules"] for g in ("all", "any") for c in r.get(g, [])})


@pytest.mark.parametrize("field", all_rule_fields())
def test_every_rule_field_is_reachable_from_a_device(field):
    """A rule reading a field no device payload can produce would silently never fire."""
    flat_targets = set(FLAT_MAP.values())
    assert field in flat_targets or field.split(".")[0] in NESTED_KEYS


@pytest.mark.parametrize("field", all_rule_fields())
def test_every_rule_field_round_trips_through_flat_payload(field):
    keys = [k for k, v in FLAT_MAP.items() if v == field]
    if not keys:
        pytest.skip("field is only reachable through nested sections")
    state = normalize("t1", {keys[0]: 123}, NOW)
    assert get_path(state, field) == 123


def test_priority_and_severity_ordering_is_deterministic():
    s = healthy()
    set_path(s, "eclss.co2_ppm", 2000); set_path(s, "eclss.scrubber_status", "INACTIVE")
    set_path(s, "eclss.ppo2_kpa", 10); set_path(s, "crew.heart_rate_bpm", 150)
    rules = ENGINE.analyze(s, now=NOW)["matched_rules"]
    sev = [r["severity"] for r in rules]
    assert sev == sorted(sev, key=lambda x: -{"CRITICAL": 4, "HIGH": 3, "WARNING": 2, "INFO": 1}[x])
    assert rules[0]["rule_id"] == "ECLSS-CO2-002"
