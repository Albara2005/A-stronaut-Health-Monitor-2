from datetime import datetime, timezone

import pytest

from normalizer import NormalizationError, normalize

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def test_flat_payload_is_mapped():
    s = normalize("esp32-01", {"heart_rate_bpm": 80, "co2_ppm": 900, "co2_sensor_status": "ONLINE",
                               "data_valid": True}, NOW)
    assert s["crew"]["heart_rate_bpm"] == 80
    assert s["eclss"]["co2_ppm"] == 900
    assert s["devices"]["co2_sensor"]["status"] == "ONLINE"
    assert s["data_quality"]["valid"] is True
    assert s["devices"]["device_id"] == "esp32-01"


def test_nested_payload_passes_through_and_merges_devices():
    s = normalize("d1", {"eclss": {"co2_ppm": 5}, "devices": {"co2_sensor": {"status": "OFFLINE"}}}, NOW)
    assert s["eclss"]["co2_ppm"] == 5
    assert s["devices"]["co2_sensor"]["status"] == "OFFLINE"
    assert s["devices"]["device_id"] == "d1"


def test_missing_timestamp_uses_receive_time():
    s = normalize("d1", {}, NOW)
    assert s["timestamp"] == NOW.isoformat() == s["received_at"]


def test_device_timestamp_is_kept():
    s = normalize("d1", {"timestamp": "2026-11-18T11:00:00Z"}, NOW)
    assert s["timestamp"].startswith("2026-11-18T11:00:00")


def test_invalid_timestamp_falls_back_to_receive_time():
    assert normalize("d1", {"timestamp": "garbage"}, NOW)["timestamp"] == NOW.isoformat()


@pytest.mark.parametrize("bad", ["", "a/b", "x" * 65, "has space", "wild+card", "#", None, 5])
def test_bad_device_ids_rejected(bad):
    with pytest.raises(NormalizationError):
        normalize(bad, {}, NOW)


@pytest.mark.parametrize("raw", [[], "x", 5, None])
def test_non_object_payload_rejected(raw):
    with pytest.raises(NormalizationError):
        normalize("d1", raw, NOW)
