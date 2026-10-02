"""Planned fire-flow assurance.

A planned fire-flow assurance evaluates, ahead of a scheduled fire event,
whether the distribution network can sustain the required fire flow:

* a primary feed path from a source to the fire node must exist,
* an edge-disjoint backup path should survive the loss of any single main,
* the pump group must deliver the flow at fire start, honouring each pump's
  minimum rest interval,
* the reservoir must hold the required volume above its protected reserve,
* current and scheduled mains outages must not leave the fire node unserved.

The judgement is rendered as ``pass``, ``conditional`` or ``fail``.
``conditional`` carries precise remediation actions; the record cannot become
``ready`` until every action is explicitly confirmed.  The network snapshot
used for the judgement is frozen on the record so later topology changes can
never rewrite a historical conclusion.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta


PASS = "pass"
CONDITIONAL = "conditional"
FAIL = "fail"
READY = "ready"


@dataclass(frozen=True)
class FireNode:
    node_id: str
    source: bool = False


@dataclass(frozen=True)
class FireMain:
    main_id: str
    a: str
    b: str
    flow_l_s: float
    open: bool = True


@dataclass(frozen=True)
class FirePump:
    pump_id: str
    flow_l_s: float
    min_rest_hours: float = 0.0
    healthy: bool = True


@dataclass(frozen=True)
class PumpState:
    """Runtime pump status as observed at planning time.

    ``since_stop_hours`` is the elapsed time since a stopped pump last ran;
    a stopped pump may only be dispatched once it reaches its minimum rest.
    """

    running: bool
    since_stop_hours: float = 0.0


@dataclass(frozen=True)
class ReservoirState:
    reservoir_id: str
    current_volume_l: float
    protected_volume_l: float
    inflow_l_s: float = 0.0


@dataclass(frozen=True)
class MainOutage:
    main_id: str
    starts_at: datetime
    ends_at: datetime
    restored: bool = False


@dataclass(frozen=True)
class FireFlowPlan:
    plan_id: str
    source: str
    target: str
    fire_start: datetime
    duration_hours: float
    required_flow_l_s: float


@dataclass(frozen=True)
class NetworkSnapshot:
    """Immutable picture of the network captured when the decision is made."""

    taken_at: datetime
    nodes: tuple[FireNode, ...]
    mains: tuple[FireMain, ...]
    pumps: tuple[FirePump, ...]
    pump_states: tuple[tuple[str, PumpState], ...]
    reservoirs: tuple[ReservoirState, ...]
    outages: tuple[MainOutage, ...]
    topology_revision: int | None = None


@dataclass(frozen=True)
class RemediationAction:
    action: str
    detail: str
    required: bool = True
    confirmed: bool = False
    confirmed_at: datetime | None = None


@dataclass(frozen=True)
class AssuranceRecord:
    record_id: str
    plan_id: str
    status: str
    snapshot: NetworkSnapshot
    primary_path: tuple[str, ...]
    backup_path: tuple[str, ...]
    available_pump_flow_l_s: float
    required_pump_flow_l_s: float
    available_fire_volume_l: float
    required_fire_volume_l: float
    findings: tuple[str, ...]
    actions: tuple[RemediationAction, ...]
    decided_at: datetime
    revision: int = 0

    @property
    def ready(self) -> bool:
        return self.status == READY


def capture_snapshot(
    nodes: list[FireNode],
    mains: list[FireMain],
    pumps: list[FirePump],
    pump_states: dict[str, PumpState],
    reservoirs: list[ReservoirState],
    outages: list[MainOutage],
    now: datetime,
    topology_revision: int | None = None,
) -> NetworkSnapshot:
    missing = sorted(set(pump_states) - {p.pump_id for p in pumps})
    if missing:
        raise ValueError("unknown pump state: " + ",".join(missing))
    return NetworkSnapshot(
        taken_at=now,
        nodes=tuple(nodes),
        mains=tuple(mains),
        pumps=tuple(pumps),
        pump_states=tuple(sorted(pump_states.items())),
        reservoirs=tuple(reservoirs),
        outages=tuple(outages),
        topology_revision=topology_revision,
    )


def _adjacency(mains: tuple[FireMain, ...], blocked: frozenset[str] = frozenset()) -> dict[str, list[tuple[str, str]]]:
    graph: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for main in mains:
        if not main.open or main.main_id in blocked:
            continue
        graph[main.a].append((main.b, main.main_id))
        graph[main.b].append((main.a, main.main_id))
    return graph


def _find_path(source: str, target: str, graph: dict[str, list[tuple[str, str]]]) -> tuple[str, ...] | None:
    if source == target:
        return ()
    prev: dict[str, tuple[str, str]] = {}
    seen = {source}
    queue = deque([source])
    while queue:
        node = queue.popleft()
        for nxt, main_id in sorted(graph.get(node, [])):
            if nxt in seen:
                continue
            seen.add(nxt)
            prev[nxt] = (node, main_id)
            if nxt == target:
                path = []
                cur = target
                while cur != source:
                    parent, edge = prev[cur]
                    path.append(edge)
                    cur = parent
                return tuple(reversed(path))
            queue.append(nxt)
    return None


def _open_outage_mains(outages: tuple[MainOutage, ...], at: datetime) -> frozenset[str]:
    """Mains whose outage makes them unavailable at ``at``."""
    return frozenset(o.main_id for o in outages if o.starts_at <= at < o.ends_at and not o.restored)


def _restore_pending_mains(outages: tuple[MainOutage, ...], now: datetime, fire_start: datetime) -> frozenset[str]:
    """Mains that are out now but scheduled to be back in service by fire start."""
    return frozenset(
        o.main_id
        for o in outages
        if o.starts_at <= now < o.ends_at
        and now < o.ends_at <= fire_start
        and not o.restored
    )


def evaluate(snapshot: NetworkSnapshot, plan: FireFlowPlan) -> AssuranceRecord:
    """Render the pass / conditional / fail decision from a frozen snapshot."""
    now = snapshot.taken_at
    mains = snapshot.mains
    sources = {n.node_id for n in snapshot.nodes if n.source}
    if plan.source not in sources:
        raise ValueError("source is not a source node")
    node_ids = {n.node_id for n in snapshot.nodes}
    if plan.target not in node_ids:
        raise ValueError("unknown target node")
    if plan.duration_hours < 0 or plan.required_flow_l_s < 0:
        raise ValueError("invalid plan")

    failures: list[str] = []
    conditions: list[str] = []
    actions: list[RemediationAction] = []

    blocked_now = _open_outage_mains(snapshot.outages, now)
    blocked_at_start = _open_outage_mains(snapshot.outages, plan.fire_start)
    restore_pending = _restore_pending_mains(snapshot.outages, now, plan.fire_start)

    # --- Key network paths -------------------------------------------------
    graph_start = _adjacency(mains, blocked_at_start)

    primary = _find_path(plan.source, plan.target, graph_start)
    if primary is None:
        failures.append("no_feed_path")
        blockers = sorted(b for b in blocked_at_start if any(m.main_id == b for m in mains))
        actions.append(
            RemediationAction(
                "restore_path",
                "restore or reroute mains before fire start: " + ",".join(blockers),
            )
        )
        primary = ()

    if primary and restore_pending.intersection(primary):
        pending = sorted(restore_pending.intersection(primary))
        conditions.append("primary_path_restoration_pending")
        actions.append(
            RemediationAction(
                "confirm_restoration",
                "confirm outages returned to service before fire start: " + ",".join(pending),
            )
        )

    backup: tuple[str, ...] = ()
    if primary:
        graph_backup = _adjacency(mains, blocked_at_start | frozenset(primary))
        backup = _find_path(plan.source, plan.target, graph_backup) or ()
        if not backup:
            conditions.append("no_backup_path")
            actions.append(
                RemediationAction(
                    "restore_backup_path",
                    "provide an edge-disjoint backup feed to node " + plan.target,
                )
            )

    # --- Pump group --------------------------------------------------------
    states = dict(snapshot.pump_states)
    lead_hours = max(0.0, (plan.fire_start - now).total_seconds() / 3600.0)
    available = 0.0
    forced = 0.0
    blocked_pumps: list[tuple[str, datetime]] = []
    broken: list[str] = []
    for pump in snapshot.pumps:
        state = states.get(pump.pump_id, PumpState(running=False))
        if not pump.healthy:
            broken.append(pump.pump_id)
            continue
        forced += pump.flow_l_s
        if state.running or state.since_stop_hours + lead_hours >= pump.min_rest_hours:
            available += pump.flow_l_s
        else:
            remaining = pump.min_rest_hours - state.since_stop_hours
            blocked_pumps.append((pump.pump_id, _ready_at(now, remaining)))

    required_flow = plan.required_flow_l_s
    if broken:
        failures.append("pumps_unavailable")
        actions.append(
            RemediationAction(
                "repair_pumps",
                "return failed pumps to service: " + ",".join(sorted(broken)),
            )
        )
    if available + 0.0 < required_flow:
        if forced + 0.0 >= required_flow:
            conditions.append("pump_min_rest")
            for pump_id, ready_at in sorted(blocked_pumps):
                actions.append(
                    RemediationAction(
                        "wait_min_rest",
                        f"hold pump {pump_id} until minimum rest satisfied "
                        f"(ready {ready_at.isoformat()})",
                    )
                )
        else:
            failures.append("insufficient_pump_capacity")
            actions.append(
                RemediationAction(
                    "add_pump_capacity",
                    f"installed pump flow {forced:g} l/s below required {required_flow:g} l/s",
                )
            )

    # --- Reservoir protected volume ---------------------------------------
    required_volume = plan.required_flow_l_s * 3600.0 * plan.duration_hours
    fire_volume = 0.0
    short_reservoirs: list[str] = []
    for reservoir in snapshot.reservoirs:
        usable = max(0.0, reservoir.current_volume_l - reservoir.protected_volume_l)
        supplied = usable + reservoir.inflow_l_s * 3600.0 * plan.duration_hours
        fire_volume += max(0.0, supplied)
        if reservoir.current_volume_l < reservoir.protected_volume_l:
            short_reservoirs.append(reservoir.reservoir_id)
    for reservoir_id in sorted(short_reservoirs):
        failures.append("reservoir_below_protected")
        actions.append(
            RemediationAction(
                "refill_reservoir",
                f"refill reservoir {reservoir_id} above its protected volume",
            )
        )
    if not short_reservoirs and fire_volume + 0.0 < required_volume:
        failures.append("insufficient_reserve_volume")
        actions.append(
            RemediationAction(
                "increase_storage",
                f"available fire volume {fire_volume:g} l below required {required_volume:g} l",
            )
        )

    if failures:
        status = FAIL
    elif conditions:
        status = CONDITIONAL
    else:
        status = PASS

    return AssuranceRecord(
        record_id=plan.plan_id + "-assurance",
        plan_id=plan.plan_id,
        status=status,
        snapshot=snapshot,
        primary_path=primary,
        backup_path=backup,
        available_pump_flow_l_s=available,
        required_pump_flow_l_s=required_flow,
        available_fire_volume_l=fire_volume,
        required_fire_volume_l=required_volume,
        findings=tuple(sorted(failures + conditions)),
        actions=tuple(actions),
        decided_at=now,
    )


def _ready_at(now: datetime, remaining_hours: float) -> datetime:
    return now + timedelta(hours=remaining_hours)


def assure(
    plan: FireFlowPlan,
    nodes: list[FireNode],
    mains: list[FireMain],
    pumps: list[FirePump],
    pump_states: dict[str, PumpState],
    reservoirs: list[ReservoirState],
    outages: list[MainOutage],
    now: datetime,
    topology_revision: int | None = None,
) -> AssuranceRecord:
    """Capture the network snapshot and render the assurance decision."""
    snapshot = capture_snapshot(nodes, mains, pumps, pump_states, reservoirs, outages, now, topology_revision)
    return evaluate(snapshot, plan)


def confirm_action(record: AssuranceRecord, action: str, at: datetime) -> AssuranceRecord:
    """Confirm one remediation action.  History-bearing timestamps must advance."""
    if at < record.decided_at:
        raise ValueError("time reversal")
    matches = [a for a in record.actions if a.action == action and not a.confirmed]
    if not matches:
        raise ValueError("no outstanding action: " + action)
    updated = tuple(
        replace(a, confirmed=True, confirmed_at=at) if a is matches[0] else a
        for a in record.actions
    )
    return replace(record, actions=updated, revision=record.revision + 1)


def mark_ready(record: AssuranceRecord, at: datetime) -> AssuranceRecord:
    """Promote a conditional record to ready once every required action is confirmed.

    Failures can never be promoted; a passing record is already ready.
    """
    if at < record.decided_at:
        raise ValueError("time reversal")
    if record.status == FAIL:
        raise ValueError("failed assurance cannot be made ready")
    if record.status == READY:
        return record
    outstanding = [a for a in record.actions if a.required and not a.confirmed]
    if outstanding:
        raise ValueError("unconfirmed remediation actions: " + ",".join(sorted({a.action for a in outstanding})))
    return replace(record, status=READY, revision=record.revision + 1)
