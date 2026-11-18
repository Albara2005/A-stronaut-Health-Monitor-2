import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from expert_system import ExpertSystem, KnowledgeBaseError, main

ENGINE = ExpertSystem()
ROOT = Path(__file__).resolve().parent.parent


def sample():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "crew": {"heart_rate_bpm": 80, "spo2_percent": 98},
        "eclss": {"co2_ppm": 700, "scrubber_status": "ACTIVE",
                  "cabin_pressure_kpa": 101.3, "ppo2_kpa": 20.5},
        "environment": {"radiation_level": 0.2},
        "devices": {
            "co2_sensor": {"status": "ONLINE"},
            "pressure_sensor": {"status": "ONLINE"},
            "radiation_sensor": {"status": "ONLINE"}
        },
        "data_quality": {"valid": True}
    }


def ids(result):
    return {r["rule_id"] for r in result["matched_rules"]}


def test_nominal_has_no_telemetry_rule_match():
    result = ENGINE.analyze(sample())
    assert result["overall_status"] == "NOMINAL"
    assert result["matched_rule_count"] == 0


def test_co2_warning_and_critical_rules():
    data = sample()
    data["eclss"]["co2_ppm"] = 1300
    data["eclss"]["scrubber_status"] = "INACTIVE"
    result = ENGINE.analyze(data)
    assert {"ECLSS-CO2-001", "ECLSS-CO2-002"} <= ids(result)
    assert result["overall_status"] == "CRITICAL"
    assert result["matched_rules"][0]["rule_id"] == "ECLSS-CO2-002"  # most severe first


def test_co2_high_alone_is_only_warning():
    data = sample()
    data["eclss"]["co2_ppm"] = 1300
    result = ENGINE.analyze(data)
    assert ids(result) == {"ECLSS-CO2-001"}
    assert result["overall_status"] == "WARNING"


def test_sensor_offline_any_group():
    data = sample()
    data["devices"]["co2_sensor"]["status"] = "OFFLINE"
    assert "SENSOR-001" in ids(ENGINE.analyze(data))


def test_string_comparison_is_case_insensitive():
    data = sample()
    data["devices"]["pressure_sensor"]["status"] = "offline"
    data["eclss"]["co2_ppm"] = 1300
    data["eclss"]["scrubber_status"] = "inactive"
    found = ids(ENGINE.analyze(data))
    assert "SENSOR-001" in found and "ECLSS-CO2-002" in found


def test_biomedical_indicators():
    data = sample()
    data["crew"]["spo2_percent"] = 89
    result = ENGINE.analyze(data)
    assert "CREW-SPO2-001" in ids(result)
    assert result["overall_status"] == "HIGH"  # HIGH severity is no longer reported as CRITICAL


def test_low_pressure_and_oxygen_and_radiation_and_hr():
    data = sample()
    data["eclss"]["cabin_pressure_kpa"] = 85
    data["eclss"]["ppo2_kpa"] = 17
    data["environment"]["radiation_level"] = 2.0
    data["crew"]["heart_rate_bpm"] = 150
    result = ENGINE.analyze(data)
    assert {"ECLSS-PRESSURE-001", "ECLSS-O2-001", "RAD-001", "CREW-HR-001"} <= ids(result)
    assert result["overall_status"] == "CRITICAL"


def test_data_quality_flag():
    data = sample()
    data["data_quality"]["valid"] = False
    assert "DATA-001" in ids(ENGINE.analyze(data))


def test_stale_telemetry():
    data = sample()
    data["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    result = ENGINE.analyze(data)
    assert result["data_quality"]["stale"] is True
    assert "DATA-TIME-002" in ids(result)


def test_missing_and_invalid_timestamp_is_flagged():
    data = sample()
    data.pop("timestamp")
    assert "DATA-TIME-001" in ids(ENGINE.analyze(data))
    data["timestamp"] = "not-a-date"
    assert "DATA-TIME-001" in ids(ENGINE.analyze(data))


def test_future_timestamp_is_flagged():
    data = sample()
    data["timestamp"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = ENGINE.analyze(data)
    assert result["data_quality"]["future_timestamp"] is True
    assert "DATA-TIME-003" in ids(result)


def test_missing_fields_do_not_match_and_are_explained():
    result = ENGINE.analyze({"timestamp": datetime.now(timezone.utc).isoformat()})
    assert result["matched_rule_count"] == 0
    co2 = next(r for r in result["evaluated_rules"] if r["rule_id"] == "ECLSS-CO2-001")
    assert "missing" in co2["conditions"][0]["reason"]


def test_explanations_exist_for_matches():
    data = sample()
    data["eclss"]["cabin_pressure_kpa"] = 85
    result = ENGINE.analyze(data)
    assert result["matched_rules"][0]["explanation"]
    assert result["matched_rules"][0]["recommendation"]


def test_fingerprint_is_stable_for_same_situation():
    a = ENGINE.analyze(sample())
    b = ENGINE.analyze(sample(), now=datetime.now(timezone.utc) + timedelta(seconds=3))
    assert a["decision_id"] == b["decision_id"]
    data = sample()
    data["eclss"]["co2_ppm"] = 1300
    assert ENGINE.analyze(data)["decision_id"] != a["decision_id"]


def test_naive_now_is_accepted():
    ENGINE.analyze(sample(), now=datetime.now(timezone.utc).replace(tzinfo=None))


def test_non_dict_telemetry_rejected():
    with pytest.raises(TypeError):
        ENGINE.analyze([1, 2, 3])


def test_incomparable_types_do_not_crash():
    data = sample()
    data["eclss"]["co2_ppm"] = "very high"
    result = ENGINE.analyze(data)
    assert "ECLSS-CO2-001" not in ids(result)


def test_disabled_rule_is_skipped():
    kb = json.loads((ROOT / "knowledge_base.json").read_text(encoding="utf-8"))
    for r in kb["rules"]:
        if r["rule_id"] == "ECLSS-CO2-001":
            r["enabled"] = False
    data = sample()
    data["eclss"]["co2_ppm"] = 1300
    assert "ECLSS-CO2-001" not in ids(ExpertSystem(kb).analyze(data))


def test_status():
    s = ENGINE.status()
    assert s["status"] == "ONLINE" and s["total_rules"] == s["enabled_rules"] == 9


# ---- knowledge base validation -------------------------------------------------

def _kb(rule):
    return {"version": "t", "rules": [rule]}


@pytest.mark.parametrize("rule", [
    {"rule_id": "X", "severity": "CRITICAL"},                                   # no conditions
    {"rule_id": "X", "all": [{"field": "a", "operator": "~", "value": 1}]},      # bad operator
    {"rule_id": "X", "severity": "SCARY", "all": [{"field": "a", "operator": ">", "value": 1}]},
    {"rule_id": "X", "priority": "high", "all": [{"field": "a", "operator": ">", "value": 1}]},
    {"rule_id": "X", "all": [{"field": "a", "operator": ">"}]},                  # no value
    {"name": "no id", "all": [{"field": "a", "operator": ">", "value": 1}]},
])
def test_invalid_rules_rejected(rule):
    with pytest.raises(KnowledgeBaseError):
        ExpertSystem(_kb(rule))


def test_duplicate_rule_ids_rejected():
    r = {"rule_id": "X", "all": [{"field": "a", "operator": ">", "value": 1}]}
    with pytest.raises(KnowledgeBaseError):
        ExpertSystem({"rules": [r, dict(r)]})


# ---- CLI + shipped example -----------------------------------------------------

def test_cli_with_sample_file(capsys):
    rc = main([str(ROOT / "examples" / "sample_telemetry.json"),
               "--as-of", "2026-11-15T08:00:05Z"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["overall_status"] == "CRITICAL"
    assert "ECLSS-CO2-002" in {r["rule_id"] for r in out["matched_rules"]}
    assert "evaluated_rules" not in out
