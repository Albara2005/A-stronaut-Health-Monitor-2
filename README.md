# AERO-MED | ORION-IV — Updated Expert System

This package provides a modular, rule-based Expert System component intended to be integrated into the existing ORION-IV backend.

## Contents
- `expert_system.py`: forward-chaining rule evaluator, explanation traces, freshness checks, deterministic severity/priority ordering.
- `knowledge_base.json`: editable IF/THEN rules and demonstration thresholds.
- `normalizer.py`: maps raw device payloads (flat or nested) to the normalized schema.
- `pipeline.py`: the single path for every input: normalize -> analyze -> persist -> publish.
- `mqtt_bridge.py`: MQTT subscriber/publisher around the pipeline (paho-mqtt 2.x).
- `storage.py`: SQLite decision history (deduplicated by fingerprint).
- `orion_app.py`: FastAPI backend (HTTP + MQTT + history API + CORS for the React frontend).
- `frontend/index.html`: dashboard (single file, no build step, Arabic/English, light/dark), served by the backend at `/`.
- `simulator.py`: fake device for demos/tests (MQTT or HTTP).
- `firmware/orion_esp32_mqtt/`: ESP32 Arduino sketch publishing over MQTT.
- `mosquitto/`, `docker-compose.yml`, `Dockerfile`, `.env.example`: run the whole stack.
- `examples/sample_telemetry.json`: normalized input example.
- `tests/`: automated tests (engine, knowledge-base audit, normalizer, MQTT bridge, API) + `dom_smoke_test.js` (browser-level check of the dashboard).

## Architecture
```
ESP32 --MQTT orion/<id>/telemetry--> Mosquitto --> MqttBridge --+
ESP32/other --HTTP POST /api/embedded/telemetry---------------+--> Pipeline
                                                                 normalize -> ExpertSystem -> SQLite
React <--HTTP GET /api/expert/latest|decisions|status----------- Pipeline
ESP32/dashboards <--MQTT orion/<id>/decision (retained)--------- MqttBridge
```

## Requirements
Python 3.10+ recommended.

```bash
python -m pip install -r requirements-dev.txt
```

## Run tests
From this directory (`pytest` and `python -m pytest` both work):

```bash
pytest -q
```

## Command line
The sample file has a fixed timestamp, so it would be reported as stale today. Use `--as-of` to evaluate it at the time it was recorded:

```bash
python expert_system.py examples/sample_telemetry.json --as-of 2026-11-15T08:00:05Z
```
Add `--full` for the complete per-rule evaluation trace.

## Run the full stack
### Option A - Docker
```bash
docker compose up --build        # broker on :1883, backend on :8000
```
### Option B - locally
```bash
# 1) a broker (Mosquitto): install it, or `docker compose up mosquitto`
# 2) backend
cp .env.example .env             # optional; defaults work for localhost
uvicorn orion_app:app --reload
# 3) fake device
python simulator.py --scenario co2            # over MQTT
python simulator.py --scenario spo2 --transport http --device-id sim-02
```
Scenarios: `nominal`, `co2`, `spo2`, `pressure`, `sensor_offline`. If the broker is down the backend still starts (HTTP keeps working) and the MQTT client keeps reconnecting in the background; `/health` shows `mqtt_connected`.

Then open **http://localhost:8000** - the dashboard shows live telemetry, matched rules with recommendations, data freshness and history, and has demo buttons that POST test telemetry. If the page is served from another origin (e.g. a Vite dev server on :5173) open it with `?api=http://localhost:8000`; CORS for :5173/:3000 is already allowed (`ORION_CORS_ORIGINS`).

Backend settings: `ORION_API_KEY` (if set, `POST /api/embedded/telemetry` requires header `X-API-Key`; the demo buttons will then get 401), `ORION_WATCHDOG_SECONDS` (default 10: re-evaluates every known device so a device that went silent is flagged `DATA-TIME-002` even though it sends nothing; 0 disables).

## MQTT topics
| Topic | Direction | Notes |
|---|---|---|
| `orion/<device_id>/telemetry` | device -> backend | JSON, max 16 KB. The device id comes from the topic, not the payload. |
| `orion/<device_id>/decision` | backend -> anyone | compact decision, QoS 1, retained (a new dashboard gets the latest immediately) |
| `orion/backend/status` | backend LWT | `online` / `offline`, retained |

Device payload: flat keys (`heart_rate_bpm`, `spo2_percent`, `co2_ppm`, `cabin_pressure_kpa`, `co2_sensor_status`, `data_valid`, ... see `FLAT_MAP` in `normalizer.py`) or already-nested sections (`crew`, `eclss`, `environment`, `devices`, `data_quality`). If the device sends no timestamp (an ESP32 without NTP), the server receive time is used; a device that does send one keeps it, so stale/future-clock detection still works.

## HTTP API
| Endpoint | Purpose |
|---|---|
| `POST /api/embedded/telemetry` | HTTP fallback ingest (`{"device_id", "timestamp"?, "data": {...}}`), analyzes immediately |
| `POST /api/expert/analyze?device_id=` | re-run the engine on the last telemetry (409 if none) |
| `GET /api/expert/overview?device_id=` | everything the dashboard needs in one call (devices, telemetry, fresh decision, history, health) |
| `GET /api/expert/telemetry?device_id=` | latest normalized telemetry |
| `GET /api/expert/latest?device_id=` | latest stored compact decision |
| `GET /api/expert/decisions?device_id=&limit=` | decision history (stored only when the situation changes) |
| `GET /api/expert/devices` / `/status` / `/health` | devices seen / engine status / liveness |

## Integrating into your existing backend
`orion_app.py` is a standalone app so it never overwrites your `main.py`. To merge: copy `expert_system.py`, `knowledge_base.json`, `normalizer.py`, `pipeline.py`, `storage.py`, `mqtt_bridge.py` beside your `main.py`, then in your lifespan create `Pipeline(ExpertSystem(), Storage(path))`, `MqttBridge(pipeline).start()`, and copy the routes you need from `orion_app.py`. Keep your existing routes and response contracts unless you intentionally version a change.

## ESP32
Open `firmware/orion_esp32_mqtt/orion_esp32_mqtt.ino`, install `PubSubClient` and `ArduinoJson` (v7), set Wi-Fi, `MQTT_HOST` (the PC's LAN IP, not `localhost`) and `DEVICE_ID`. Sensor reads are placeholders until you wire your MAX30102 / temperature sensor. This sketch was not compiled or tested on hardware here.

## Security note
The bundled Mosquitto config allows anonymous local connections for development only. Before any real deployment enable username/password and TLS (`MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS=1`).

## Quick local test
```python
import json
from expert_system import ExpertSystem

engine = ExpertSystem()
telemetry = json.load(open("examples/sample_telemetry.json", encoding="utf-8"))
result = engine.analyze(telemetry)
print(json.dumps(result, indent=2))
```

## Normalized telemetry schema
The rules expect dotted paths such as:
- `eclss.co2_ppm`
- `eclss.scrubber_status`
- `eclss.cabin_pressure_kpa`
- `eclss.ppo2_kpa`
- `environment.radiation_level`
- `crew.heart_rate_bpm`
- `crew.spo2_percent`
- `devices.co2_sensor.status`
- `data_quality.valid`
- top-level `timestamp` in ISO-8601 format (prefer UTC with `Z`).

Map your existing backend telemetry model into this normalized schema before calling `engine.analyze(state)`. Do not assume your current database/API uses these exact field names.

## Important engineering notes
- Missing telemetry fields do not cause a rule to match; conditions evaluate false and are explained as missing.
- Missing or invalid timestamps create a data-quality finding.
- Old telemetry creates a stale-data finding.
- Multiple rules may match simultaneously.
- Overall status is the highest matched severity: `CRITICAL` > `HIGH` > `WARNING` > `INFO` > `NOMINAL`. Among matches, results are ordered by severity, then numeric priority (lower number = more urgent), then rule ID.
- The knowledge base is validated on load (unique `rule_id`, known severity, supported operators, at least one condition per rule, required `value`). A broken rule raises `KnowledgeBaseError` instead of silently misbehaving - previously a rule with no conditions would match every telemetry.
- String comparisons are case-insensitive (`"offline"` matches `"OFFLINE"`).
- Timestamp findings: `DATA-TIME-001` missing/invalid, `DATA-TIME-002` stale, `DATA-TIME-003` timestamp in the future (clock problem).
- Decision fingerprint is a deduplication aid, not a secure unique identifier. It ignores volatile values such as the telemetry age, so the same situation gives the same fingerprint on repeated calls.
- The current rule thresholds are illustrative demonstration values. They are NOT validated NASA, medical, or spacecraft operating limits.
- This system is decision-support / educational software only. It must not control life-support equipment or make clinical decisions.
