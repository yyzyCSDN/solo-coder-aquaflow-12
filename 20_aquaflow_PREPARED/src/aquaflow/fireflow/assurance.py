"""Planned fire-flow assurance.

Evaluates whether a planned fire demand at a site can be satisfied at a future
moment, considering:

* critical network paths (primary and edge-disjoint backup routes),
* pump group capability, honouring minimum rest intervals,
* reservoir protected storage,
* current outage state (permanent failures versus confirmed restorations).

Each decision embeds an immutable :class:`NetworkSnapshot` taken at evaluation
time.  Replaying a stored snapshot always reproduces the original outcome, so
later topology changes never rewrite history.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from heapq import heappush, heappop

from ..network.pathing import Junction, Main

#: Flow (L/s) carried per square millimetre of main cross-section.
CAPACITY_FACTOR = 0.0015


@dataclass(frozen=True)
class FirePump:
    """A pump available for the planned fire demand."""

    id: str
    flow_l_s: float
    available: bool
    running: bool
    since_stop: float
    min_rest: float

    def dispatchable(self) -> bool:
        return self.available and (self.running or self.since_stop >= self.min_rest)

    def resting(self) -> bool:
        return self.available and not self.running and self.since_stop < self.min_rest

    def wait_minutes(self) -> float:
        return max(0.0, self.min_rest - self.since_stop)


@dataclass(frozen=True)
class ProtectedReservoir:
    """Storage that may be drawn down to its protected level."""

    id: str
    available_volume_l: float
    protected_volume_l: float
    inflow_l_s: float = 0.0


@dataclass(frozen=True)
class Outage:
    """A main outage. ``restored_at`` is set once restoration is confirmed."""

    main_id: str
    restored_at: datetime | None = None


@dataclass(frozen=True)
class NetworkSnapshot:
    """Immutable inputs frozen at the moment of evaluation."""

    captured_at: datetime
    site_id: str
    required_flow_l_s: float
    duration_s: float
    starts_at: datetime
    nodes: tuple[Junction, ...]
    mains: tuple[Main, ...]
    pumps: tuple[FirePump, ...]
    reservoirs: tuple[ProtectedReservoir, ...]
    outages: tuple[Outage, ...]


@dataclass(frozen=True)
class RemediationAction:
    """Precise action that must be confirmed before readiness."""

    code: str
    ref: str
    detail: str


@dataclass(frozen=True)
class AssuranceRecord:
    record_id: str
    outcome: str  # "passed" | "conditional" | "failed"
    reasons: tuple[str, ...]
    actions: tuple[RemediationAction, ...]
    confirmed_actions: frozenset[str]
    ready: bool
    snapshot: NetworkSnapshot
    metrics: dict[str, object] = field(default_factory=dict)
    version: int = 1
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Network evaluation
# ---------------------------------------------------------------------------

def main_capacity_l_s(main: Main) -> float:
    return CAPACITY_FACTOR * main.diameter_mm * main.diameter_mm * main.condition


def _usable_mains(mains: tuple[Main, ...], blocked: frozenset[str]) -> dict[str, list[tuple[str, Main]]]:
    adj: dict[str, list[tuple[str, Main]]] = {}
    for main in mains:
        if not main.open or main.main_id in blocked:
            continue
        adj.setdefault(main.a, []).append((main.b, main))
        adj.setdefault(main.b, []).append((main.a, main))
    return adj


def widest_path(
    sources: frozenset[str],
    target: str,
    adj: dict[str, list[tuple[str, Main]]],
    excluded: frozenset[str] = frozenset(),
) -> tuple[float, tuple[str, ...]] | None:
    """Maximum bottleneck-capacity route from any source to ``target``.

    Ties break by fewer mains then lexicographically smaller main id sequence.
    Returns ``(bottleneck_l_s, main_ids)`` or ``None`` when unreachable.
    """
    if target in sources:
        return float("inf"), ()
    best_cap: dict[str, float] = {source: float("inf") for source in sources}
    best_path: dict[str, tuple[str, ...]] = {source: () for source in sources}
    # Max-heap on bottleneck capacity; ties favour shorter then
    # lexicographically smaller main sequences.
    heap: list[tuple["_HeapKey", str]] = []
    for source in sorted(sources):
        heappush(heap, (_HeapKey(float("inf"), ()), source))
    while heap:
        key, node = heappop(heap)
        cap, path = key.cap, key.path
        if path != best_path.get(node):
            continue
        if node == target:
            return cap, path
        for nxt, main in adj.get(node, []):
            if main.main_id in excluded:
                continue
            new_cap = min(cap, main_capacity_l_s(main))
            new_path = path + (main.main_id,)
            current_cap = best_cap.get(nxt)
            if current_cap is None or new_cap > current_cap or (
                new_cap == current_cap and _better(new_path, best_path[nxt])
            ):
                best_cap[nxt] = new_cap
                best_path[nxt] = new_path
                heappush(heap, (_HeapKey(new_cap, new_path), nxt))
    return None


class _HeapKey:
    __slots__ = ("cap", "path")

    def __init__(self, cap: float, path: tuple[str, ...]):
        self.cap = cap
        self.path = path

    def __lt__(self, other: "_HeapKey") -> bool:
        if self.cap != other.cap:
            return self.cap > other.cap
        if len(self.path) != len(other.path):
            return len(self.path) < len(other.path)
        return self.path < other.path


def _better(path: tuple[str, ...], current: tuple[str, ...]) -> bool:
    if len(path) != len(current):
        return len(path) < len(current)
    return path < current


def _pump_selection(
    pumps: tuple[FirePump, ...], required: float
) -> tuple[tuple[str, ...] | None, tuple[str, ...], float]:
    """Return (selected ids, resting pumps blocking capacity, shortfall)."""
    selected = [p for p in pumps if p.dispatchable()]
    selected.sort(key=lambda p: (-p.flow_l_s, p.id))
    chosen: list[str] = []
    capacity = 0.0
    for pump in selected:
        if capacity >= required:
            break
        chosen.append(pump.id)
        capacity += pump.flow_l_s
    resting = tuple(p.id for p in pumps if p.resting())
    if capacity >= required:
        return tuple(chosen), resting, 0.0
    total = capacity + sum(p.flow_l_s for p in pumps if p.resting())
    if total >= required:
        return None, resting, 0.0
    return None, resting, required - total


def _reservoir_check(
    reservoirs: tuple[ProtectedReservoir, ...], required: float, duration_s: float
) -> tuple[bool, float]:
    required_volume = required * duration_s
    usable = 0.0
    for reservoir in reservoirs:
        usable += max(0.0, reservoir.available_volume_l - reservoir.protected_volume_l)
        usable += reservoir.inflow_l_s * duration_s
    return usable >= required_volume, max(0.0, required_volume - usable)


def evaluate_snapshot(snapshot: NetworkSnapshot) -> AssuranceRecord:
    """Apply the assurance rules to an already captured snapshot."""
    nodes = snapshot.nodes
    mains = snapshot.mains
    sources = frozenset(n.node_id for n in nodes if n.source)
    required = snapshot.required_flow_l_s

    node_ids = {n.node_id for n in nodes}
    if snapshot.site_id not in node_ids:
        raise ValueError("unknown site")
    if not sources:
        raise ValueError("no source")
    if required <= 0 or snapshot.duration_s <= 0:
        raise ValueError("invalid demand")

    # Current outage state.  Restoration is never assumed on an ETA alone:
    # every outage keeps its main blocked for the network evaluation.  An ETA
    # before the planned start turns the blocker into a confirmable
    # remediation action instead of a hard failure.
    blocked: set[str] = {o.main_id for o in snapshot.outages}
    pending_outage: dict[str, Outage] = {o.main_id: o for o in snapshot.outages}
    blocked_frozen = frozenset(blocked)

    reasons: list[str] = []
    actions: list[RemediationAction] = []
    metrics: dict[str, object] = {}

    full_adj = _usable_mains(mains, frozenset())
    adj = _usable_mains(mains, blocked_frozen)
    primary = widest_path(sources, snapshot.site_id, adj)

    if primary is None:
        # Attribute the disconnect: if a route exists with outages cleared,
        # the outages on that route are the precise cause.  Restoration ETAs
        # before the planned start become confirmable actions; anything still
        # open at the start is a hard failure.
        unblocked = widest_path(sources, snapshot.site_id, full_adj)
        if unblocked is None:
            reasons.append("no_supply_path")
        else:
            _, would_be_path = unblocked
            for main_id in sorted(set(would_be_path) & blocked_frozen):
                outage = pending_outage[main_id]
                if outage.restored_at is not None and outage.restored_at <= snapshot.starts_at:
                    actions.append(
                        RemediationAction(
                            "confirm_outage_restoration",
                            main_id,
                            f"confirm restoration of main {main_id} before {snapshot.starts_at.isoformat()}",
                        )
                    )
                else:
                    reasons.append(f"active_outage:{main_id}")
            # Even once the outage clears, a sole route still lacks redundancy.
            if snapshot.site_id not in sources:
                if not any(
                    m in blocked_frozen
                    and pending_outage[m].restored_at is not None
                    and pending_outage[m].restored_at <= snapshot.starts_at
                    for m in would_be_path
                ):
                    backup_route = widest_path(
                        sources, snapshot.site_id, full_adj,
                        excluded=frozenset(would_be_path),
                    )
                else:
                    backup_route = None
                if backup_route is None and not any(
                    a.code == "provide_backup_path" for a in actions
                ):
                    actions.append(
                        RemediationAction(
                            "provide_backup_path",
                            snapshot.site_id,
                            "install or open an edge-disjoint backup feed to "
                            f"{snapshot.site_id}; primary route uses {'-'.join(would_be_path) or 'no mains'}",
                        )
                    )
    else:
        primary_cap, primary_path = primary
        metrics["primary_path"] = list(primary_path)
        metrics["primary_capacity_l_s"] = round(primary_cap, 6)
        if primary_cap < required:
            reasons.append("primary_path_capacity")
        # A single main taking out the primary route must not defeat the fire
        # demand: require an edge-disjoint backup from a (possibly different)
        # source.
        if snapshot.site_id not in sources:
            backup = widest_path(sources, snapshot.site_id, adj, excluded=frozenset(primary_path))
            if backup is None:
                actions.append(
                    RemediationAction(
                        "provide_backup_path",
                        snapshot.site_id,
                        f"install or open an edge-disjoint backup feed to {snapshot.site_id}; "
                        f"primary route uses {'-'.join(primary_path) or 'no mains'}",
                    )
                )
            else:
                backup_cap, backup_path = backup
                metrics["backup_path"] = list(backup_path)
                metrics["backup_capacity_l_s"] = round(backup_cap, 6)
                if backup_cap < required:
                    reasons.append("backup_path_capacity")

    # Outages still active at the planned start only fail if they sit on a
    # route the assurance must rely on.
    if primary is not None:
        relied_on = set(metrics.get("primary_path", ())) | set(metrics.get("backup_path", ()))
        for main_id in sorted(relied_on & blocked_frozen):
            outage = pending_outage[main_id]
            if outage.restored_at is not None and outage.restored_at <= snapshot.starts_at:
                # Restoration is promised but not yet confirmed in the field.
                actions.append(
                    RemediationAction(
                        "confirm_outage_restoration",
                        main_id,
                        f"confirm restoration of main {main_id} before {snapshot.starts_at.isoformat()}",
                    )
                )
            else:
                reasons.append(f"active_outage:{main_id}")

    # Pump group capability, including minimum rest intervals.
    chosen, resting, shortfall = _pump_selection(snapshot.pumps, required)
    metrics["pumps_selected"] = list(chosen) if chosen is not None else []
    metrics["pumps_in_min_rest"] = list(resting)
    if chosen is None:
        if resting and shortfall == 0.0:
            for pump in sorted((p for p in snapshot.pumps if p.resting()), key=lambda p: p.id):
                actions.append(
                    RemediationAction(
                        "wait_pump_min_rest",
                        pump.id,
                        f"hold start until pump {pump.id} clears its minimum rest interval "
                        f"({pump.wait_minutes():g} time units remaining)",
                    )
                )
        else:
            reasons.append("insufficient_pump_capacity")
            metrics["pump_shortfall_l_s"] = round(shortfall, 6)

    # Reservoir protected storage.
    storage_ok, deficit = _reservoir_check(snapshot.reservoirs, required, snapshot.duration_s)
    metrics["reservoir_storage_deficit_l"] = round(deficit, 6)
    if not storage_ok:
        reasons.append("protected_volume_breach")

    if reasons:
        outcome = "failed"
        actions = []
    elif actions:
        outcome = "conditional"
    else:
        outcome = "passed"

    return AssuranceRecord(
        record_id="",
        outcome=outcome,
        reasons=tuple(reasons),
        actions=tuple(sorted(actions, key=lambda a: (a.code, a.ref))),
        confirmed_actions=frozenset(),
        ready=outcome == "passed",
        snapshot=snapshot,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Snapshot capture, lifecycle, replay
# ---------------------------------------------------------------------------

def capture_snapshot(
    site_id: str,
    required_flow_l_s: float,
    duration_s: float,
    starts_at: datetime,
    nodes: list[Junction],
    mains: list[Main],
    pumps: list[FirePump],
    reservoirs: list[ProtectedReservoir],
    outages: list[Outage],
    now: datetime,
) -> NetworkSnapshot:
    if starts_at < now:
        raise ValueError("planned start in the past")
    return NetworkSnapshot(
        captured_at=now,
        site_id=site_id,
        required_flow_l_s=float(required_flow_l_s),
        duration_s=float(duration_s),
        starts_at=starts_at,
        nodes=tuple(nodes),
        mains=tuple(mains),
        pumps=tuple(pumps),
        reservoirs=tuple(reservoirs),
        outages=tuple(outages),
    )


def assure(
    site_id: str,
    required_flow_l_s: float,
    duration_s: float,
    starts_at: datetime,
    nodes: list[Junction],
    mains: list[Main],
    pumps: list[FirePump],
    reservoirs: list[ProtectedReservoir],
    outages: list[Outage],
    now: datetime,
    record_id: str,
) -> AssuranceRecord:
    """Capture a network snapshot and produce the assurance record."""
    snapshot = capture_snapshot(
        site_id, required_flow_l_s, duration_s, starts_at,
        nodes, mains, pumps, reservoirs, outages, now,
    )
    record = evaluate_snapshot(snapshot)
    return replace(record, record_id=record_id, updated_at=now)


def action_keys(actions: tuple[RemediationAction, ...]) -> list[str]:
    return [f"{a.code}:{a.ref}" for a in actions]


def confirm_action(
    record: AssuranceRecord, code: str, ref: str, at: datetime
) -> AssuranceRecord:
    """Mark one remediation action confirmed; readiness requires all of them."""
    if record.outcome == "failed":
        raise ValueError("failed record cannot be remediated")
    key = f"{code}:{ref}"
    known = set(action_keys(record.actions))
    if key not in known:
        raise ValueError("unknown remediation action")
    if record.updated_at is not None and at < record.updated_at:
        raise ValueError("time reversal")
    confirmed = set(record.confirmed_actions)
    confirmed.add(key)
    all_confirmed = confirmed >= known
    return replace(
        record,
        confirmed_actions=frozenset(confirmed),
        ready=all_confirmed,
        version=record.version + 1,
        updated_at=at,
    )


def replay_snapshot(snapshot: NetworkSnapshot) -> AssuranceRecord:
    """Re-evaluate a stored snapshot. Always reproduces the historical call."""
    return evaluate_snapshot(snapshot)


class FireFlowRegistry:
    """Stores assurance records keyed by id, preserving their snapshots."""

    def __init__(self) -> None:
        self._records: dict[str, AssuranceRecord] = {}

    def save(self, record: AssuranceRecord) -> AssuranceRecord:
        if not record.record_id:
            raise ValueError("record requires an id")
        self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> AssuranceRecord:
        return self._records[record_id]

    def confirm_action(
        self, record_id: str, code: str, ref: str, at: datetime
    ) -> AssuranceRecord:
        record = confirm_action(self._records[record_id], code, ref, at)
        self._records[record_id] = record
        return record

    def replay(self, record_id: str) -> AssuranceRecord:
        """Re-run the rules against the frozen snapshot of a stored record."""
        stored = self._records[record_id]
        fresh = replay_snapshot(stored.snapshot)
        if fresh.outcome != stored.outcome or list(fresh.reasons) != list(stored.reasons):
            raise AssertionError("historical conclusion changed")
        return fresh

    def __len__(self) -> int:
        return len(self._records)
