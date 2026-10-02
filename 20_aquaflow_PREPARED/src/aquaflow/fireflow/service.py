"""Application service for planned fire-flow assurances."""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ..network.pathing import Junction, Main
from .assurance import (
    AssuranceRecord,
    FireFlowRegistry,
    FirePump,
    Outage,
    ProtectedReservoir,
    assure,
    replay_snapshot,
)


class FireFlowService:
    """Evaluate planned fire-flow demands and track remediation to readiness."""

    def __init__(self) -> None:
        self.registry = FireFlowRegistry()

    def plan(
        self,
        site_id: str,
        required_flow_l_s: float,
        duration_s: float,
        starts_at: datetime,
        nodes: list[Junction],
        mains: list[Main],
        pumps: list[FirePump],
        reservoirs: list[ProtectedReservoir],
        outages: list[Outage] | None = None,
        now: datetime | None = None,
        record_id: str | None = None,
    ) -> AssuranceRecord:
        record = assure(
            site_id,
            required_flow_l_s,
            duration_s,
            starts_at,
            nodes,
            mains,
            pumps,
            reservoirs,
            list(outages or []),
            now or datetime.now().astimezone(),
            record_id or uuid4().hex,
        )
        return self.registry.save(record)

    def confirm_action(self, record_id: str, code: str, ref: str, at: datetime) -> AssuranceRecord:
        return self.registry.confirm_action(record_id, code, ref, at)

    def get(self, record_id: str) -> AssuranceRecord:
        return self.registry.get(record_id)

    def replay(self, record_id: str) -> AssuranceRecord:
        """Re-evaluate the frozen snapshot; topology changes cannot alter it."""
        return self.registry.replay(record_id)

    def replay_snapshot(self, record: AssuranceRecord) -> AssuranceRecord:
        return replay_snapshot(record.snapshot)
