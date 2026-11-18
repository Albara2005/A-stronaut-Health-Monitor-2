"""Fake device for demos/tests. Publishes JSON telemetry over MQTT (default) or HTTP.

    python simulator.py --scenario nominal
    python simulator.py --scenario co2 --count 5 --interval 2
    python simulator.py --transport http --url http://localhost:8000 --scenario spo2
Scenarios: nominal | co2 | spo2 | pressure | sensor_offline
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timezone

SCENARIOS = {
    "nominal": {},
    "co2": {"co2_ppm": 1300, "scrubber_status": "INACTIVE"},
    "spo2": {"spo2_percent": 89},
    "pressure": {"cabin_pressure_kpa": 85},
    "sensor_offline": {"co2_sensor_status": "OFFLINE"},
}


def build_payload(scenario: str) -> dict:
    p = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "heart_rate_bpm": 78, "spo2_percent": 98, "skin_temp_c": 36.6,
        "co2_ppm": 700, "scrubber_status": "ACTIVE",
        "cabin_pressure_kpa": 101.3, "ppo2_kpa": 20.5, "radiation_level": 0.2,
        "co2_sensor_status": "ONLINE", "pressure_sensor_status": "ONLINE",
        "radiation_sensor_status": "ONLINE", "data_valid": True,
    }
    p.update(SCENARIOS[scenario])
    return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=SCENARIOS, default="nominal")
    ap.add_argument("--device-id", default="sim-01")
    ap.add_argument("--transport", choices=("mqtt", "http"), default="mqtt")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--prefix", default="orion")
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()

    client = None
    if a.transport == "mqtt":
        import paho.mqtt.client as mqtt
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{a.device_id}")
        client.connect(a.host, a.port)
        client.loop_start()

    for i in range(a.count):
        payload = build_payload(a.scenario)
        if client:
            topic = f"{a.prefix}/{a.device_id}/telemetry"
            client.publish(topic, json.dumps(payload), qos=1).wait_for_publish(5)
            print(f"[{i+1}/{a.count}] published to {topic}")
        else:
            body = json.dumps({"device_id": a.device_id, "data": payload}).encode()
            req = urllib.request.Request(f"{a.url}/api/embedded/telemetry", body,
                                         {"Content-Type": "application/json"})
            print(f"[{i+1}/{a.count}]", urllib.request.urlopen(req, timeout=5).read().decode())
        if i + 1 < a.count:
            time.sleep(a.interval)
    if client:
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
