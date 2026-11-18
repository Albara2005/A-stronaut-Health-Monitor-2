"""Map raw device payloads (flat or nested) into the normalized schema used by knowledge_base.json."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from expert_system import parse_time, utc_now

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # also safe as an MQTT topic level

# flat key sent by the device -> dotted path in the normalized schema
FLAT_MAP = {
    "heart_rate_bpm": "crew.heart_rate_bpm", "heart_rate": "crew.heart_rate_bpm", "bpm": "crew.heart_rate_bpm",
    "spo2_percent": "crew.spo2_percent", "spo2": "crew.spo2_percent",
    "skin_temp_c": "crew.skin_temp_c", "temperature_c": "crew.skin_temp_c",
    "accel_g": "crew.accel_g",
    "co2_ppm": "eclss.co2_ppm", "scrubber_status": "eclss.scrubber_status",
    "cabin_pressure_kpa": "eclss.cabin_pressure_kpa", "pressure_kpa": "eclss.cabin_pressure_kpa",
    "ppo2_kpa": "eclss.ppo2_kpa",
    "radiation_level": "environment.radiation_level",
    "co2_sensor_status": "devices.co2_sensor.status",
    "pressure_sensor_status": "devices.pressure_sensor.status",
    "radiation_sensor_status": "devices.radiation_sensor.status",
    "data_valid": "data_quality.valid",
}
NESTED_KEYS = ("crew", "eclss", "environment", "devices", "data_quality")


class NormalizationError(ValueError):
    pass


def validate_device_id(device_id: Any) -> str:
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        raise NormalizationError("device_id must match [A-Za-z0-9_.-]{1,64}")
    return device_id


def _set_path(target: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = target[part] = {}
        target = nxt
    target[parts[-1]] = value


def _deep_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def normalize(device_id: str, raw: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Return a normalized telemetry state.

    - Nested sections (crew/eclss/environment/devices/data_quality) pass through.
    - Flat keys listed in FLAT_MAP are moved to their dotted path (nested values win on conflict).
    - If the device sent no (or an invalid) timestamp - typical for an ESP32 without NTP - the server
      receive time is used, because MQTT/HTTP delivery is near-instant. A device that DOES send a
      timestamp keeps it, so stale/future-clock detection still works for it.
    """
    device_id = validate_device_id(device_id)
    if not isinstance(raw, dict):
        raise NormalizationError("telemetry payload must be a JSON object")
    now = parse_time(now) if now else utc_now()

    state: Dict[str, Any] = {}
    for key, path in FLAT_MAP.items():
        if key in raw and not isinstance(raw[key], (dict, list)):
            _set_path(state, path, raw[key])
    for key in NESTED_KEYS:
        if isinstance(raw.get(key), dict):
            _deep_merge(state.setdefault(key, {}), copy.deepcopy(raw[key]))

    stamp = parse_time(raw.get("timestamp"))
    state["timestamp"] = (stamp or now).isoformat()
    state["received_at"] = now.isoformat()
    state.setdefault("devices", {})["device_id"] = device_id
    return state
