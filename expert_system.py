"""
AERO-MED | ORION-IV
Rule-based Expert System (educational / engineering demonstration).

Features:
- JSON Knowledge Base (validated on load)
- Forward chaining over telemetry facts
- all/any condition groups
- priorities and deterministic conflict ordering
- missing / stale / future-dated telemetry checks
- sensor fault rules
- explanation traces
- stable decision deduplication fingerprint
- no autonomous medical or spacecraft control
"""
from __future__ import annotations

import argparse
import hashlib
import json
import operator as op
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class KnowledgeBaseError(ValueError):
    """Raised when the knowledge base is malformed."""


def _norm(value: Any) -> Any:
    """Case-insensitive comparison for strings (so 'offline' == 'OFFLINE')."""
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, (list, tuple, set)):
        return [_norm(v) for v in value]
    return value


OPS = {
    "==": lambda a, b: _norm(a) == _norm(b),
    "!=": lambda a, b: _norm(a) != _norm(b),
    ">": op.gt, ">=": op.ge, "<": op.lt, "<=": op.le,
    "in": lambda a, b: _norm(a) in _norm(b),
    "not_in": lambda a, b: _norm(a) not in _norm(b),
    "contains": lambda a, b: _norm(b) in _norm(a),
}

SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "WARNING": 2, "INFO": 1}
FUTURE_TOLERANCE_SECONDS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_path(data: Dict[str, Any], path: str) -> Any:
    """Read dotted paths, e.g. eclss.co2_ppm or crew.EV-01.heart_rate."""
    current: Any = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def validate_knowledge_base(kb: Any) -> None:
    """Fail fast on a broken knowledge base instead of misbehaving at runtime."""
    if not isinstance(kb, dict):
        raise KnowledgeBaseError("Knowledge base must be a JSON object")
    rules = kb.get("rules")
    if not isinstance(rules, list):
        raise KnowledgeBaseError("Knowledge base must contain a 'rules' list")
    seen = set()
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, dict) or not rule.get("rule_id"):
            raise KnowledgeBaseError(f"{where}: missing 'rule_id'")
        rid = rule["rule_id"]
        if rid in seen:
            raise KnowledgeBaseError(f"{rid}: duplicate rule_id")
        seen.add(rid)
        sev = rule.get("severity", "INFO")
        if sev not in SEVERITY_RANK:
            raise KnowledgeBaseError(f"{rid}: unknown severity {sev!r}")
        try:
            int(rule.get("priority", 100))
        except (TypeError, ValueError):
            raise KnowledgeBaseError(f"{rid}: priority must be an integer") from None
        conditions = list(rule.get("all") or []) + list(rule.get("any") or [])
        if not conditions:
            # Without this, a rule with no conditions would match every telemetry.
            raise KnowledgeBaseError(f"{rid}: rule needs at least one condition in 'all' or 'any'")
        for c in conditions:
            if not isinstance(c, dict) or "field" not in c or "operator" not in c:
                raise KnowledgeBaseError(f"{rid}: each condition needs 'field' and 'operator'")
            if c["operator"] not in OPS:
                raise KnowledgeBaseError(f"{rid}: unsupported operator {c['operator']!r}")
            if "value" not in c:
                raise KnowledgeBaseError(f"{rid}: condition on {c['field']!r} is missing 'value'")


@dataclass
class ConditionResult:
    field: str
    operator: str
    expected: Any
    actual: Any
    passed: bool
    reason: str


class ExpertSystem:
    def __init__(self, knowledge_base: Any = None, rules_path: Optional[str] = None):
        if knowledge_base is None:
            path = Path(rules_path) if rules_path else Path(__file__).with_name("knowledge_base.json")
            knowledge_base = json.loads(path.read_text(encoding="utf-8"))
        validate_knowledge_base(knowledge_base)
        self.kb = knowledge_base
        self.rules = list(self.kb.get("rules", []))
        self.settings = self.kb.get("settings", {})
        self.kb_version = self.kb.get("version", "unknown")

    def status(self) -> Dict[str, Any]:
        return {
            "status": "ONLINE",
            "engine": "ORION-IV Rule-Based Inference Engine",
            "knowledge_base_version": self.kb_version,
            "total_rules": len(self.rules),
            "enabled_rules": sum(1 for r in self.rules if r.get("enabled", True)),
            "reasoning": "FORWARD_CHAINING",
            "data_mode": "DEMONSTRATION_RULES",
        }

    def _condition(self, telemetry: Dict[str, Any], condition: Dict[str, Any]) -> ConditionResult:
        field = condition["field"]
        operator_name = condition["operator"]
        expected = condition.get("value")
        actual = get_path(telemetry, field)
        if actual is None:
            return ConditionResult(field, operator_name, expected, None, False,
                                   f"{field} is missing; condition cannot be evaluated")
        if operator_name not in OPS:
            return ConditionResult(field, operator_name, expected, actual, False,
                                   f"Unsupported operator: {operator_name}")
        try:
            passed = bool(OPS[operator_name](actual, expected))
            reason = (f"{field}={actual!r} {operator_name} {expected!r}: "
                      f"{'TRUE' if passed else 'FALSE'}")
        except (TypeError, ValueError):
            passed = False
            reason = f"{field} could not be compared with {operator_name} {expected!r}"
        return ConditionResult(field, operator_name, expected, actual, passed, reason)

    def _evaluate_group(self, telemetry: Dict[str, Any], conditions: List[Dict[str, Any]],
                        mode: str) -> Tuple[bool, List[ConditionResult]]:
        results = [self._condition(telemetry, c) for c in conditions]
        if not results:
            return False, results
        if mode == "any":
            return any(x.passed for x in results), results
        return all(x.passed for x in results), results

    @staticmethod
    def _telemetry_offset_seconds(telemetry: Dict[str, Any], now: datetime) -> Optional[float]:
        """Seconds since the telemetry timestamp (negative = timestamp is in the future)."""
        stamp = parse_time(telemetry.get("timestamp") or telemetry.get("received_at"))
        if stamp is None:
            return None
        return (now - stamp).total_seconds()

    def analyze(self, telemetry: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not isinstance(telemetry, dict):
            raise TypeError("telemetry must be a dictionary")
        now = parse_time(now) if now is not None else utc_now()
        matched: List[Dict[str, Any]] = []
        evaluated = []
        for rule in self.rules:
            if not rule.get("enabled", True):
                continue
            all_ok, all_results = self._evaluate_group(
                telemetry, rule.get("all", []), "all"
            ) if rule.get("all") else (True, [])
            any_ok, any_results = self._evaluate_group(
                telemetry, rule.get("any", []), "any"
            ) if rule.get("any") else (True, [])
            conditions_ok = all_ok and any_ok
            result_rows = all_results + any_results
            row = {
                "rule_id": rule["rule_id"],
                "name": rule.get("name", rule["rule_id"]),
                "matched": conditions_ok,
                "conditions": [asdict(x) for x in result_rows],
            }
            evaluated.append(row)
            if conditions_ok:
                matched.append({
                    "rule_id": rule["rule_id"],
                    "name": rule.get("name", rule["rule_id"]),
                    "category": rule.get("category", "GENERAL"),
                    "priority": int(rule.get("priority", 100)),
                    "severity": rule.get("severity", "INFO"),
                    "recommendation": rule.get("recommendation", ""),
                    "explanation": rule.get("explanation", ""),
                    "conditions": [asdict(x) for x in result_rows],
                    "rule_version": rule.get("version", self.kb_version),
                })

        # Generic freshness / timestamp diagnostics.
        max_age = int(self.settings.get("max_telemetry_age_seconds", 30))
        offset = self._telemetry_offset_seconds(telemetry, now)
        age = None if offset is None else max(0.0, offset)
        future = offset is not None and offset < -FUTURE_TOLERANCE_SECONDS
        data_quality = {"timestamp_present": offset is not None, "age_seconds": age,
                        "stale": age is not None and age > max_age,
                        "future_timestamp": future,
                        "max_age_seconds": max_age}
        if offset is None:
            matched.append(self._system_finding(
                "DATA-TIME-001", "Telemetry timestamp unavailable", "WARNING", 5,
                "Provide a valid ISO-8601 timestamp with timezone.",
                "The telemetry timestamp is missing or invalid; freshness cannot be verified."
            ))
        elif future:
            matched.append(self._system_finding(
                "DATA-TIME-003", "Telemetry timestamp is in the future", "WARNING", 5,
                "Check the clock synchronisation (NTP) of the telemetry source.",
                f"Telemetry is dated {int(-offset)} seconds ahead of the engine clock; freshness cannot be trusted."
            ))
        elif age > max_age:
            matched.append(self._system_finding(
                "DATA-TIME-002", "Stale telemetry detected", "WARNING", 4,
                "Check device connectivity and telemetry delivery.",
                f"Telemetry is {int(age)} seconds old; configured freshness limit is {max_age} seconds."
            ))

        # Deterministic conflict ordering: severity first, then numeric priority
        # (lower number = more urgent), then rule ID.
        matched.sort(key=lambda x: (-SEVERITY_RANK.get(x["severity"], 0),
                                    x["priority"], x["rule_id"]))
        overall = "NOMINAL"
        if matched:
            overall = max((x["severity"] for x in matched),
                          key=lambda s: SEVERITY_RANK.get(s, 0))

        decision = {
            "decision_id": None,
            "created_at": now.isoformat(),
            "overall_status": overall,
            "matched_rule_count": len(matched),
            "matched_rules": matched,
            "evaluated_rules": evaluated,
            "data_quality": data_quality,
            "explanation": self._overall_explanation(overall, matched, data_quality),
            "disclaimer": (
                "Educational and engineering decision-support demonstration only. "
                "Rules and thresholds are not validated medical, NASA, or spacecraft operating limits. "
                "Do not use for autonomous control or clinical decisions."
            ),
        }
        decision["decision_id"] = self._fingerprint(decision)
        return decision

    @staticmethod
    def _system_finding(rule_id: str, name: str, severity: str, priority: int,
                        recommendation: str, explanation: str) -> Dict[str, Any]:
        return {
            "rule_id": rule_id, "name": name, "category": "DATA_QUALITY",
            "priority": priority, "severity": severity,
            "recommendation": recommendation, "explanation": explanation,
            "conditions": [], "rule_version": "SYSTEM",
        }

    @staticmethod
    def _overall_explanation(status: str, matched: List[Dict[str, Any]],
                             quality: Dict[str, Any]) -> str:
        if not matched:
            return "No enabled demonstration rule matched the supplied telemetry. This does not prove the system is safe."
        ids = ", ".join(x["rule_id"] for x in matched)
        return f"Overall status is {status}. Matched rules: {ids}. Review each rule's conditions and recommendation."

    @staticmethod
    def _fingerprint(decision: Dict[str, Any]) -> str:
        """Deduplication aid. Excludes volatile values (age in seconds, timestamps) so
        the same situation produces the same fingerprint on repeated calls."""
        quality = decision["data_quality"]
        payload = {
            "status": decision["overall_status"],
            "rules": [(x["rule_id"], x["severity"]) for x in decision["matched_rules"]],
            "quality": {k: quality[k] for k in
                        ("timestamp_present", "stale", "future_timestamp", "max_age_seconds")},
        }
        raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]


def analyze_telemetry(telemetry: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience function for existing FastAPI code (prefer one shared ExpertSystem instance)."""
    return ExpertSystem().analyze(telemetry)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ORION-IV expert system on a telemetry JSON file.")
    parser.add_argument("telemetry", help="Path to telemetry JSON, e.g. examples/sample_telemetry.json")
    parser.add_argument("--as-of", help="Evaluate as if 'now' were this ISO-8601 time "
                                        "(useful for the static sample file, whose timestamp ages).")
    parser.add_argument("--rules", help="Path to an alternative knowledge_base.json")
    parser.add_argument("--full", action="store_true", help="Include the full evaluated_rules trace")
    args = parser.parse_args(argv)

    telemetry = json.loads(Path(args.telemetry).read_text(encoding="utf-8"))
    now = parse_time(args.as_of) if args.as_of else None
    if args.as_of and now is None:
        parser.error(f"invalid --as-of value: {args.as_of!r}")
    result = ExpertSystem(rules_path=args.rules).analyze(telemetry, now=now)
    if not args.full:
        result.pop("evaluated_rules", None)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
