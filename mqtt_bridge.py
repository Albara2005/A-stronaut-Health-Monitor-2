"""MQTT <-> expert system bridge.

Topics (prefix defaults to "orion"):
    orion/<device_id>/telemetry   device  -> backend   JSON telemetry (QoS 0/1)
    orion/<device_id>/decision    backend -> anyone    compact decision (QoS 1, retained)
    orion/backend/status          backend LWT          "online" / "offline" (retained)

Environment: MQTT_HOST, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TOPIC_PREFIX, MQTT_TLS=1
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

import paho.mqtt.client as mqtt

from normalizer import NormalizationError
from pipeline import Pipeline

log = logging.getLogger("orion.mqtt")
MAX_PAYLOAD_BYTES = 16 * 1024


class MqttBridge:
    def __init__(self, pipeline: Pipeline, *, host: Optional[str] = None, port: Optional[int] = None,
                 username: Optional[str] = None, password: Optional[str] = None,
                 prefix: Optional[str] = None, tls: Optional[bool] = None,
                 client_id: str = "orion-backend"):
        self.pipeline = pipeline
        self.host = host or os.getenv("MQTT_HOST", "localhost")
        self.port = int(port or os.getenv("MQTT_PORT", 1883))
        self.prefix = (prefix or os.getenv("MQTT_TOPIC_PREFIX", "orion")).strip("/")
        username = username or os.getenv("MQTT_USERNAME")
        password = password or os.getenv("MQTT_PASSWORD")
        tls = tls if tls is not None else os.getenv("MQTT_TLS", "0") == "1"

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username:
            self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set()
        self.client.will_set(self.status_topic, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.connected = False
        pipeline.publisher = self  # decisions flow back out through this bridge

    # ---- topics -------------------------------------------------------------
    @property
    def status_topic(self) -> str:
        return f"{self.prefix}/backend/status"

    @property
    def telemetry_filter(self) -> str:
        return f"{self.prefix}/+/telemetry"

    def decision_topic(self, device_id: str) -> str:
        return f"{self.prefix}/{device_id}/decision"

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Non-blocking: connects in the background and keeps retrying if the broker is down."""
        log.info("MQTT connecting to %s:%s", self.host, self.port)
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            if self.connected:
                self.client.publish(self.status_topic, "offline", qos=1, retain=True).wait_for_publish(2)
        finally:
            self.client.loop_stop()
            self.client.disconnect()

    # ---- callbacks ----------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connect refused: %s", reason_code)
            return
        self.connected = True
        client.subscribe(self.telemetry_filter, qos=1)  # (re)subscribe on every reconnect
        client.publish(self.status_topic, "online", qos=1, retain=True)
        log.info("MQTT connected; subscribed to %s", self.telemetry_filter)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        self.handle_message(msg.topic, msg.payload)

    def handle_message(self, topic: str, payload: bytes) -> Optional[Dict[str, Any]]:
        """Process one telemetry message. Never raises: a bad message must not kill the network loop."""
        parts = topic.split("/")
        prefix_parts = self.prefix.split("/")
        n = len(prefix_parts)
        if len(parts) != n + 2 or parts[:n] != prefix_parts or parts[-1] != "telemetry":
            log.debug("ignoring topic %s", topic)
            return None
        device_id = parts[n]  # the topic is authoritative for device identity
        if len(payload) > MAX_PAYLOAD_BYTES:
            log.warning("payload from %s too large (%d bytes)", device_id, len(payload))
            return None
        try:
            raw = json.loads(payload.decode("utf-8"))
            return self.pipeline.ingest(device_id, raw, source="mqtt")
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.warning("invalid JSON from %s", device_id)
        except NormalizationError as exc:
            log.warning("rejected telemetry from %s: %s", device_id, exc)
        except Exception:  # noqa: BLE001 - keep the loop alive, log the traceback
            log.exception("unexpected error while processing telemetry from %s", device_id)
        return None

    # ---- DecisionPublisher --------------------------------------------------
    def publish_decision(self, device_id: str, summary: Dict[str, Any]) -> None:
        if not self.connected:
            return
        self.client.publish(self.decision_topic(device_id), json.dumps(summary), qos=1, retain=True)
