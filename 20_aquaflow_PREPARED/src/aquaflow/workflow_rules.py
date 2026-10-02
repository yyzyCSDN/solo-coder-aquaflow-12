"""Domain rules used by the cross-module operational workflow service."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .booster.failover import Unit, choose as choose_boosters
from .chemicals.inventory import Lot, active_mass
from .flush.plan import Hydrant, waves
from .incidents.escalation import Incident, Rule, overdue
from .lab.custody import Event, validate as validate_custody
from .lab.revisions import Result, current
from .lab.scheduling import Collector, Req, assign
from .leak.localize import Signal, rank
from .meters.balance import Meter, balance
from .network.isolation import Pipe, isolated
from .network.pathing import Junction, Main
from .network.pressure import Node as PressureNode, classify as classify_pressure
from .network.topology import Revision, at as topology_at
from .fireflow.assurance import (
    FirePump,
    Outage,
    ProtectedReservoir,
    assure as assure_fire_flow,
)
from .outage.impact import Customer, summarize
from .pumps.schedule import Hist, Pump, choose as choose_pumps
from .regulatory.rolling import Reading, average
from .reservoir.turnover import State, project_age
from .treatment.backwash import Filter, choose as choose_filter
from .treatment.chlorine_ct import Segment, effective_ct
from .treatment.coagulant import Jar, recommend
from .work.confined import Permit, valid


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _serialize_snapshot(record: Any) -> dict[str, Any]:
    """Freeze the inputs used for the decision into audit-friendly data."""
    snap = record.snapshot
    return {
        "captured_at": snap.captured_at.isoformat(),
        "site_id": snap.site_id,
        "required_flow_l_s": snap.required_flow_l_s,
        "duration_s": snap.duration_s,
        "starts_at": snap.starts_at.isoformat(),
        "nodes": [n.node_id for n in snap.nodes],
        "mains": [
            {"id": m.main_id, "a": m.a, "b": m.b, "open": m.open,
             "diameter_mm": m.diameter_mm, "condition": m.condition}
            for m in snap.mains
        ],
        "pumps": [
            {"id": p.id, "flow_l_s": p.flow_l_s, "available": p.available,
             "running": p.running, "since_stop": p.since_stop, "min_rest": p.min_rest}
            for p in snap.pumps
        ],
        "reservoirs": [
            {"id": r.id, "available_volume_l": r.available_volume_l,
             "protected_volume_l": r.protected_volume_l, "inflow_l_s": r.inflow_l_s}
            for r in snap.reservoirs
        ],
        "outages": [
            {"main_id": o.main_id,
             "restored_at": o.restored_at.isoformat() if o.restored_at else None}
            for o in snap.outages
        ],
    }


def evaluate(name: str, p: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one workflow and return a stable decision plus evidence."""
    if name == "ct_assurance":
        rows = [Segment(float(x["minutes"]), float(x["residual"]), float(x["baffling"]), float(x["temperature_c"])) for x in p["segments"]]
        ct = effective_ct(rows)
        dose = p["dose"]
        low, high = float(dose["low_limit"]), float(dose["high_limit"])
        residual_ok = low <= float(dose["mg_l"]) <= high
        return {"decision": "release" if residual_ok and ct >= float(p.get("required", 0)) else "hold", "value": ct, "evidence": {"ct": ct, "residual_ok": residual_ok}}
    if name == "coagulant_match":
        target = p["target"]
        jars = [Jar(float(x["raw_ntu"]), float(x["alkalinity"]), float(x["temp"]), float(x["dose"]), float(x["settled_ntu"])) for x in p["jars"]]
        dose = recommend(jars, float(target["ntu"]), float(target["alkalinity"]), float(target["temp"]))
        return {"decision": "dose", "value": dose, "evidence": {"candidate_count": len(jars)}}
    if name == "filter_backwash":
        rows = [Filter(str(x["id"]), float(x["headloss"]), float(x["turbidity"]), _dt(x["last_backwash"]), bool(x.get("online", True))) for x in p["filters"]]
        selected = choose_filter(rows, _dt(p["now"]), int(p["max_offline"]), set(p["offline"]))
        return {"decision": "backwash" if selected else "defer", "value": selected, "evidence": {"offline": sorted(p["offline"])}}
    if name == "reservoir_age":
        s = p["state"]
        age = project_age(State(float(s["volume"]), float(s["inflow"]), float(s["outflow"]), float(s["age_hours"])), float(p["hours"]))
        return {"decision": "renew" if age <= float(p.get("max_age", 24)) else "hold", "value": age, "evidence": {"fresh_inflow": float(s["inflow"])}}
    if name == "network_isolation":
        ps = [Pipe(str(x["id"]), str(x["a"]), str(x["b"]), bool(x.get("open", True))) for x in p["pipes"]]
        cut = isolated(set(p["nodes"]), ps, set(p["closed"]), set(p["sources"]))
        return {"decision": "isolate", "value": sorted(cut), "evidence": {"source_count": len(p["sources"])}}
    if name == "zone_pressure":
        rows = [PressureNode(str(x["id"]), float(x["elevation_m"]), float(x["pressure_kpa"])) for x in p["nodes"]]
        result = classify_pressure(rows, float(p["low"]), float(p["high"]))
        return {"decision": "intervene" if result["low"] or result["high"] else "monitor", "value": result, "evidence": {"nodes": len(rows)}}
    if name == "pump_dispatch":
        rows = [Pump(str(x["id"]), float(x["kw"]), float(x["flow"]), float(x["min_run"]), float(x["min_rest"])) for x in p["pumps"]]
        history = {str(k): Hist(bool(v["running"]), float(v["since_start"]), float(v["since_stop"])) for k, v in p["history"].items()}
        chosen = choose_pumps(rows, history, float(p["required"]), float(p["max_kw"]))
        return {"decision": "dispatch", "value": chosen, "evidence": {"capacity": sum(x["flow"] for x in p["pumps"] if x["id"] in chosen)}}
    if name == "booster_failover":
        rows = [Unit(str(x["id"]), bool(x["healthy"]), int(x["rank"]), float(x["capacity"])) for x in p["units"]]
        chosen = choose_boosters(rows, float(p["required"]), str(p["failed"]))
        return {"decision": "reroute", "value": chosen, "evidence": {"failed": p["failed"]}}
    if name == "custody_chain":
        events = [Event(int(x["seq"]), str(x["src"]), str(x["dst"]), _dt(x["at"]), str(x["seal"])) for x in p["events"]]
        end = validate_custody(str(p["origin"]), events)
        return {"decision": "accept", "value": end, "evidence": {"events": len(events)}}
    if name == "collector_schedule":
        requests = [Req(str(x["site"]), _dt(x["earliest"]), _dt(x["latest"]), int(x["minutes"]), bool(x["certified"])) for x in p["requests"]]
        people = [Collector(str(x["name"]), bool(x["certified"]), _dt(x["start"]), _dt(x["end"])) for x in p["collectors"]]
        result = assign(requests, people)
        return {"decision": "schedule", "value": {k: [v[0], v[1].isoformat()] for k, v in result.items()}, "evidence": {"requests": len(requests)}}
    if name == "lab_revision":
        rows = [Result(str(x["sample"]), str(x["analyte"]), float(x["value"]), int(x["revision"]), x.get("supersedes")) for x in p["results"]]
        result = current(rows, str(p["sample"]), str(p["analyte"]))
        return {"decision": "publish", "value": result.revision, "evidence": {"value": result.value}}
    if name == "meter_balance":
        ins = [Meter(str(x["id"]), Decimal(str(x["opening"])), Decimal(str(x["closing"])), Decimal(str(x["multiplier"]))) for x in p["inputs"]]
        outs = [Meter(str(x["id"]), Decimal(str(x["opening"])), Decimal(str(x["closing"])), Decimal(str(x["multiplier"]))) for x in p["outputs"]]
        result = balance(ins, outs, Decimal(str(p["known_loss"])))
        return {"decision": "reconcile" if result >= 0 else "investigate", "value": result, "evidence": {"inputs": len(ins), "outputs": len(outs)}}
    if name == "leak_localization":
        rows = [Signal(str(x["sensor"]), str(x["pipe"]), float(x["drop"]), float(x["acoustic"]), float(x["night_delta"])) for x in p["signals"]]
        result = rank(rows)
        return {"decision": "inspect" if result else "clear", "value": result, "evidence": {"sensors": len(rows)}}
    if name == "flush_waves":
        rows = [Hydrant(str(x["id"]), float(x["flow"]), bool(x["sensitive"])) for x in p["hydrants"]]
        result = waves(rows, float(p["required"]), int(p["max_parallel"]))
        return {"decision": "execute", "value": result, "evidence": {"wave_count": len(result)}}
    if name == "chemical_active_mass":
        rows = [Lot(str(x["id"]), Decimal(str(x["gross_kg"])), Decimal(str(x["concentration"])), bool(x.get("released", True))) for x in p["lots"]]
        result = active_mass(rows)
        return {"decision": "release" if result > 0 else "hold", "value": result, "evidence": {"lots": len(rows)}}
    if name == "confined_permit":
        x = p["permit"]
        permit = Permit(_dt(x["starts"]), _dt(x["ends"]), _dt(x["gas_test"]), str(x["attendant"]), str(x["entrant"]), str(x["rescue"]))
        result = valid(permit, _dt(p["at"]), int(p["max_age"]))
        return {"decision": "enter" if result else "deny", "value": result, "evidence": {"role_separation": permit.attendant != permit.entrant}}
    if name == "outage_impact":
        rows = [Customer(str(x["account"]), str(x["node"]), bool(x["critical"]), int(x["population"])) for x in p["customers"]]
        result = summarize(rows, set(p["nodes"]))
        return {"decision": "notify" if result["population"] else "clear", "value": result, "evidence": {"nodes": sorted(p["nodes"])}}
    if name == "incident_escalation":
        i = p["incident"]
        incident = Incident(int(i["severity"]), _dt(i["opened"]), _dt(i["ack"]) if i.get("ack") else None)
        rules = [Rule(int(x["severity"]), int(x["minutes"]), str(x["role"])) for x in p["rules"]]
        result = overdue(incident, rules, _dt(p["now"]))
        return {"decision": "escalate" if result else "standby", "value": sorted(result), "evidence": {"severity": incident.severity}}
    if name == "topology_revision":
        rows = [Revision(int(x["revision"]), int(x["sequence"]), frozenset(x["open_pipes"])) for x in p["revisions"]]
        result = topology_at(int(p["sequence"]), rows)
        return {"decision": "use", "value": result.revision, "evidence": {"open_pipes": sorted(result.open_pipes)}}
    if name == "regulatory_window":
        rows = [Reading(_dt(x["at"]), float(x["value"])) for x in p["readings"]]
        result = average(rows, timedelta(hours=float(p["window_hours"])), _dt(p["at"]))
        return {"decision": "report" if result is not None else "defer", "value": result, "evidence": {"reading_count": len(rows)}}
    if name == "fire_flow_assurance":
        site = p["site"]
        nodes = [Junction(str(x["id"]), float(x["elevation_m"]), float(x.get("demand_l_s", 0.0)), bool(x.get("source", False)), bool(x.get("critical", False))) for x in p["nodes"]]
        mains = [Main(str(x["id"]), str(x["a"]), str(x["b"]), float(x["length_m"]), float(x["diameter_mm"]), bool(x.get("open", True)), float(x.get("condition", 1.0))) for x in p["mains"]]
        pumps = [FirePump(str(x["id"]), float(x["flow_l_s"]), bool(x.get("available", True)), bool(x.get("running", False)), float(x.get("since_stop", 0.0)), float(x.get("min_rest", 0.0))) for x in p["pumps"]]
        reservoirs = [ProtectedReservoir(str(x["id"]), float(x["available_volume_l"]), float(x["protected_volume_l"]), float(x.get("inflow_l_s", 0.0))) for x in p["reservoirs"]]
        outages = [Outage(str(x["main_id"]), _dt(x["restored_at"]) if x.get("restored_at") else None) for x in p["outages"]]
        now, starts_at = _dt(p["now"]), _dt(p["starts_at"])
        record = assure_fire_flow(
            str(site) if isinstance(site, str) else str(site["id"]),
            float(p["required_flow_l_s"]),
            float(p["duration_s"]),
            starts_at,
            nodes,
            mains,
            pumps,
            reservoirs,
            outages,
            now,
            record_id=str(p.get("record_id", "")),
        )
        return {
            "decision": {"passed": "ready", "conditional": "remediate", "failed": "deny"}[record.outcome],
            "value": record.outcome,
            "evidence": {
                "reasons": list(record.reasons),
                "actions": [{"code": a.code, "ref": a.ref, "detail": a.detail} for a in record.actions],
                "ready": record.ready,
                "metrics": record.metrics,
                "snapshot": _serialize_snapshot(record),
            },
        }
    raise ValueError("unknown workflow")

