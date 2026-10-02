"""Input contracts for cross-module operational workflows.

The workflow layer deliberately keeps transport concerns separate from domain
rules.  Each request is normalized once, so downstream modules can reason
about typed records and preserve an audit-friendly input snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class WorkflowEnvelope:
    name: str
    payload: dict[str, Any]
    received_at: datetime


_REQUIRED: dict[str, tuple[str, ...]] = {
    "ct_assurance": ("segments", "dose"),
    "coagulant_match": ("jars", "target"),
    "filter_backwash": ("filters", "now", "offline", "max_offline"),
    "reservoir_age": ("state", "hours"),
    "network_isolation": ("nodes", "pipes", "closed", "sources"),
    "zone_pressure": ("nodes", "low", "high"),
    "pump_dispatch": ("pumps", "history", "required", "max_kw"),
    "booster_failover": ("units", "required", "failed"),
    "custody_chain": ("origin", "events"),
    "collector_schedule": ("requests", "collectors"),
    "lab_revision": ("results", "sample", "analyte"),
    "meter_balance": ("inputs", "outputs", "known_loss"),
    "leak_localization": ("signals",),
    "flush_waves": ("hydrants", "required", "max_parallel"),
    "chemical_active_mass": ("lots",),
    "confined_permit": ("permit", "at", "max_age"),
    "outage_impact": ("customers", "nodes"),
    "incident_escalation": ("incident", "rules", "now"),
    "topology_revision": ("sequence", "revisions"),
    "regulatory_window": ("readings", "window_hours", "at"),
    "fire_flow_assurance": ("site", "required_flow_l_s", "duration_s", "now", "starts_at", "nodes", "mains", "pumps", "reservoirs", "outages"),
}


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_copy(v) for v in value)
    return value


def normalize(name: str, payload: dict[str, Any], received_at: datetime | None = None) -> WorkflowEnvelope:
    if name not in _REQUIRED:
        raise ValueError("unknown workflow")
    if not isinstance(payload, dict):
        raise TypeError("workflow payload must be a mapping")
    missing = [key for key in _REQUIRED[name] if key not in payload]
    if missing:
        raise ValueError("missing workflow fields: " + ",".join(missing))
    normalized = _copy(payload)
    normalized["_contract"] = "v1"
    normalized["_required"] = _REQUIRED[name]
    return WorkflowEnvelope(name, normalized, received_at or datetime.now().astimezone())
