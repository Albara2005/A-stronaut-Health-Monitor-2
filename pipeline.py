"""One shared path for every input (MQTT, HTTP): normalize -> analyze -> persist -> publish."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol

from expert_system import ExpertSystem
from normalizer import normalize
from storage import Storage


class DecisionPublisher(Protocol):
    def publish_decision(self, device_id: str, summary: Dict[str, Any]) -> None: ...


def summarize(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Compact version of a decision for MQTT / dashboards (no per-condition trace)."""
    return {
        "decision_id": decision["decision_id"],
        "created_at": decision["created_at"],
        "overall_status": decision["overall_status"],
        "explanation": decision["explanation"],
        "matched_rules": [
            {k: r.get(k, "") for k in ("rule_id", "name", "severity", "priority",
                                        "recommendation", "explanation")}
            for r in decision["matched_rules"]
        ],
        "data_quality": decision["data_quality"],
        "disclaimer": decision["disclaimer"],
    }


class Pipeline:
    def __init__(self, engine: ExpertSystem, storage: Storage,
                 publisher: Optional[DecisionPublisher] = None):
        self.engine = engine
        self.storage = storage
        self.publisher = publisher
        self._lock = threading.Lock()
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._published: Dict[str, str] = {}   # device_id -> last published decision_id
        self._last_device: Optional[str] = None

    # ---- internals ----------------------------------------------------------
    def _publish(self, device_id: str, decision: Dict[str, Any], only_if_changed: bool) -> None:
        if self.publisher is None:
            return
        with self._lock:
            if only_if_changed and self._published.get(device_id) == decision["decision_id"]:
                return
            self._published[device_id] = decision["decision_id"]
        self.publisher.publish_decision(device_id, summarize(decision))

    # ---- public API ---------------------------------------------------------
    def ingest(self, device_id: str, raw: Any, *, source: str = "http",
               now: Optional[datetime] = None) -> Dict[str, Any]:
        state = normalize(device_id, raw, now)
        with self._lock:
            self._latest[device_id] = state
            self._last_device = device_id
        decision = self.engine.analyze(state, now=now)
        stored = self.storage.save_decision(device_id, state, decision)
        self._publish(device_id, decision, only_if_changed=False)
        return {"device_id": device_id, "source": source, "stored": stored, "decision": decision}

    def reanalyze(self, device_id: Optional[str] = None, *, only_publish_changes: bool = False
                  ) -> Optional[Dict[str, Any]]:
        """Re-run the engine on the last telemetry received (default: the device that sent last)."""
        with self._lock:
            device_id = device_id or self._last_device
            state = dict(self._latest[device_id]) if device_id in self._latest else None
        if state is None:
            return None
        decision = self.engine.analyze(state)
        stored = self.storage.save_decision(device_id, state, decision)
        self._publish(device_id, decision, only_if_changed=only_publish_changes)
        return {"device_id": device_id, "source": "reanalyze", "stored": stored, "decision": decision}

    def reanalyze_all(self) -> int:
        """Watchdog tick: a device that went silent never triggers ingest(), so staleness
        (DATA-TIME-002) would never be noticed without this periodic re-evaluation."""
        ids = self.devices()
        for d in ids:
            self.reanalyze(d, only_publish_changes=True)
        return len(ids)

    def latest_telemetry(self, device_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._latest.get(device_id)
            return dict(state) if state else None

    @property
    def last_device(self) -> Optional[str]:
        return self._last_device

    def devices(self) -> List[str]:
        with self._lock:
            return list(self._latest)
