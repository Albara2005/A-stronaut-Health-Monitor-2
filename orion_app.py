"""ORION-IV backend: HTTP + MQTT ingestion, expert system, SQLite history, dashboard.

Run:  uvicorn orion_app:app --reload      then open http://localhost:8000
Env:  ORION_DB_PATH (default orion.db), ORION_MQTT_ENABLED (default 1), ORION_CORS_ORIGINS,
      ORION_API_KEY (if set, POST /api/embedded/telemetry requires header X-API-Key),
      ORION_WATCHDOG_SECONDS (default 10, 0 = off), MQTT_* (see mqtt_bridge.py)
This is a separate file on purpose: do not overwrite your existing main.py - copy the routes you need.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from expert_system import ExpertSystem, parse_time, utc_now
from normalizer import NormalizationError
from pipeline import Pipeline, summarize
from storage import Storage

log = logging.getLogger("orion.app")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
FRONTEND_DIR = Path(__file__).with_name("frontend")


class TelemetryPayload(BaseModel):
    device_id: str
    timestamp: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)


def create_app(db_path: Optional[str] = None, enable_mqtt: Optional[bool] = None,
               api_key: Optional[str] = None, watchdog_seconds: Optional[float] = None) -> FastAPI:
    db_path = db_path or os.getenv("ORION_DB_PATH", "orion.db")
    if enable_mqtt is None:
        enable_mqtt = os.getenv("ORION_MQTT_ENABLED", "1") == "1"
    api_key = api_key if api_key is not None else os.getenv("ORION_API_KEY", "")
    if watchdog_seconds is None:
        watchdog_seconds = float(os.getenv("ORION_WATCHDOG_SECONDS", "10"))

    engine = ExpertSystem()
    storage = Storage(db_path)
    pipeline = Pipeline(engine, storage)
    bridge = None
    if enable_mqtt:
        from mqtt_bridge import MqttBridge  # imported lazily so HTTP-only use needs no broker
        bridge = MqttBridge(pipeline)

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(watchdog_seconds)
            try:
                await run_in_threadpool(pipeline.reanalyze_all)
            except Exception:  # noqa: BLE001 - the watchdog must never die
                log.exception("watchdog tick failed")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if bridge:
            bridge.start()
        task = asyncio.create_task(watchdog()) if watchdog_seconds > 0 else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if bridge:
            bridge.stop()
        storage.close()

    def require_key(x_api_key: Optional[str] = Header(default=None)) -> None:
        if api_key and not hmac.compare_digest(x_api_key or "", api_key):
            raise HTTPException(401, "invalid or missing X-API-Key")

    app = FastAPI(title="ORION-IV Backend", version="1.3.0", lifespan=lifespan)
    origins = [o.strip() for o in os.getenv(
        "ORION_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])
    app.state.pipeline, app.state.engine, app.state.bridge = pipeline, engine, bridge

    def health_info() -> Dict[str, Any]:
        return {"ok": True, "mqtt_enabled": bridge is not None,
                "mqtt_connected": bool(bridge and bridge.connected)}

    @app.get("/health")
    def health():
        return health_info()

    @app.post("/api/embedded/telemetry", dependencies=[Depends(require_key)])
    def receive_telemetry(payload: TelemetryPayload):
        """HTTP fallback for devices that do not use MQTT. Analyzes immediately."""
        if payload.timestamp is not None and parse_time(payload.timestamp) is None:
            raise HTTPException(422, "timestamp must be a valid ISO-8601 string")
        raw = dict(payload.data)
        if payload.timestamp is not None:
            raw["timestamp"] = payload.timestamp
        try:
            out = pipeline.ingest(payload.device_id, raw, source="http")
        except NormalizationError as exc:
            raise HTTPException(422, str(exc)) from exc
        d = out["decision"]
        return {"accepted": True, "device_id": out["device_id"], "stored": out["stored"],
                "overall_status": d["overall_status"], "decision_id": d["decision_id"]}

    @app.post("/api/expert/analyze")
    def analyze(device_id: Optional[str] = None):
        out = pipeline.reanalyze(device_id)
        if out is None:
            raise HTTPException(409, "No telemetry has been received")
        return out["decision"]

    @app.get("/api/expert/latest")
    def latest(device_id: Optional[str] = None):
        d = storage.latest(device_id)
        if d is None:
            raise HTTPException(404, "No decisions stored yet")
        return summarize(d)

    @app.get("/api/expert/telemetry")
    def telemetry(device_id: Optional[str] = None):
        device_id = device_id or pipeline.last_device
        state = (pipeline.latest_telemetry(device_id) or storage.latest_telemetry(device_id)) \
            if device_id else None
        if state is None:
            raise HTTPException(404, "No telemetry for this device")
        return state

    @app.get("/api/expert/decisions")
    def decisions(device_id: Optional[str] = None, limit: int = Query(50, ge=1, le=500)):
        return {"items": storage.history(device_id, limit)}

    @app.get("/api/expert/devices")
    def devices():
        return {"devices": list(dict.fromkeys(pipeline.devices() + storage.devices()))}

    @app.get("/api/expert/status")
    def expert_status():
        return {**engine.status(), "mqtt_connected": bool(bridge and bridge.connected)}

    @app.get("/api/expert/overview")
    def overview(device_id: Optional[str] = None):
        """Everything the dashboard needs in ONE request. The decision is computed fresh from the
        latest telemetry (not read from history), so data age / staleness is always current."""
        known = list(dict.fromkeys(pipeline.devices() + storage.devices()))
        device_id = device_id or pipeline.last_device or (known[0] if known else None)
        state = (pipeline.latest_telemetry(device_id) or storage.latest_telemetry(device_id)) \
            if device_id else None
        return {
            "server_time": utc_now().isoformat(),
            "health": health_info(),
            "engine": engine.status(),
            "devices": known,
            "device_id": device_id,
            "telemetry": state,
            "decision": summarize(engine.analyze(state)) if state else None,
            "history": storage.history(device_id, 15) if device_id else [],
        }

    if FRONTEND_DIR.is_dir():  # registered last so it never shadows an API route
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
