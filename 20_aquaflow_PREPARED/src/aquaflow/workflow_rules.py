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
from .network.fire_flow import (
    FireFlowPlan,
    FireMain,
    FireNode,
    MainOutage,
    PumpState,
    ReservoirState,
    FirePump,
    assure as assure_fire_flow,
)
from .network.pressure import Node as PressureNode, classify as classify_pressure
from .network.topology import Revision, at as topology_at
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
        plan_in = p["plan"]
        plan = FireFlowPlan(
            str(plan_in["plan_id"]),
            str(plan_in["source"]),
            str(plan_in["target"]),
            _dt(plan_in["fire_start"]),
            float(plan_in["duration_hours"]),
            float(plan_in["required_flow_l_s"]),
        )
        nodes = [FireNode(str(x["id"]), bool(x.get("source", False))) for x in p["nodes"]]
        mains = [FireMain(str(x["id"]), str(x["a"]), str(x["b"]), float(x["flow_l_s"]), bool(x.get("open", True))) for x in p["mains"]]
        pumps = [
            FirePump(str(x["id"]), float(x["flow_l_s"]), float(x.get("min_rest_hours", 0.0)), bool(x.get("healthy", True)))
            for x in p["pumps"]
        ]
        pump_states = {
            str(k): PumpState(bool(v["running"]), float(v.get("since_stop_hours", 0.0)))
            for k, v in p["pump_states"].items()
        }
        reservoirs = [
            ReservoirState(
                str(x["id"]),
                float(x["current_volume_l"]),
                float(x["protected_volume_l"]),
                float(x.get("inflow_l_s", 0.0)),
            )
            for x in p["reservoirs"]
        ]
        outages = [
            MainOutage(str(x["main_id"]), _dt(x["starts_at"]), _dt(x["ends_at"]), bool(x.get("restored", False)))
            for x in p["outages"]
        ]
        record = assure_fire_flow(
            plan, nodes, mains, pumps, pump_states, reservoirs, outages, _dt(p["now"]),
            p.get("topology_revision"),
        )
        return {
            "decision": record.status,
            "value": {
                "record_id": record.record_id,
                "primary_path": list(record.primary_path),
                "backup_path": list(record.backup_path),
                "available_pump_flow_l_s": record.available_pump_flow_l_s,
                "available_fire_volume_l": record.available_fire_volume_l,
                "findings": list(record.findings),
                "actions": [
                    {"action": a.action, "detail": a.detail, "confirmed": a.confirmed}
                    for a in record.actions
                ],
                "snapshot_revision": record.snapshot.topology_revision,
                "snapshot_taken_at": record.snapshot.taken_at.isoformat(),
            },
            "evidence": {"action_count": len(record.actions), "snapshot_mains": len(record.snapshot.mains)},
        }
    raise ValueError("unknown workflow")

