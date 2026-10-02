import unittest
from datetime import datetime, timedelta, timezone

from aquaflow.api import AquaFlowService
from aquaflow.network.fire_flow import (
    AssuranceRecord,
    FireFlowPlan,
    FireMain,
    FireNode,
    FirePump,
    MainOutage,
    NetworkSnapshot,
    PumpState,
    ReservoirState,
    assure,
    capture_snapshot,
    confirm_action,
    mark_ready,
)

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
FIRE_START = NOW + timedelta(days=1)


def loop_network():
    """Source S feeding target T over two edge-disjoint paths."""
    nodes = [FireNode("S", source=True), FireNode("A"), FireNode("B"), FireNode("T")]
    mains = [
        FireMain("m1", "S", "A", 100.0),
        FireMain("m2", "A", "T", 100.0),
        FireMain("m3", "S", "B", 100.0),
        FireMain("m4", "B", "T", 100.0),
    ]
    return nodes, mains


def radial_network():
    """Single feed path S-A-T: no edge-disjoint backup exists."""
    nodes = [FireNode("S", source=True), FireNode("A"), FireNode("T")]
    mains = [
        FireMain("m1", "S", "A", 100.0),
        FireMain("m2", "A", "T", 100.0),
    ]
    return nodes, mains


def plan(required=50.0, duration=2.0):
    return FireFlowPlan("fp-1", "S", "T", FIRE_START, duration, required)


def reservoir(current=2_000_000.0, protected=200_000.0, inflow=0.0):
    return [ReservoirState("r1", current, protected, inflow)]


class FireFlowAssuranceTests(unittest.TestCase):
    def test_pass_with_primary_and_backup_path(self):
        nodes, mains = loop_network()
        pumps = [FirePump("p1", 60.0), FirePump("p2", 60.0)]
        states = {"p1": PumpState(running=True), "p2": PumpState(running=False, since_stop_hours=10.0)}
        record = assure(plan(), nodes, mains, pumps, states, reservoir(), [], NOW, topology_revision=7)

        self.assertEqual(record.status, "pass")
        self.assertFalse(record.ready)
        self.assertEqual(tuple(record.primary_path), ("m1", "m2"))
        self.assertEqual(tuple(record.backup_path), ("m3", "m4"))
        self.assertEqual(record.available_pump_flow_l_s, 120.0)
        self.assertEqual(record.findings, ())
        self.assertEqual(record.actions, ())
        self.assertEqual(record.snapshot.topology_revision, 7)

        ready = mark_ready(record, NOW)
        self.assertEqual(ready.status, "ready")
        self.assertTrue(ready.ready)
        self.assertEqual(ready.revision, 1)

    def test_missing_backup_path_is_conditional_with_precise_action(self):
        nodes, mains = radial_network()
        pumps = [FirePump("p1", 80.0)]
        states = {"p1": PumpState(running=True)}
        record = assure(plan(), nodes, mains, pumps, states, reservoir(), [], NOW)

        self.assertEqual(record.status, "conditional")
        self.assertIn("no_backup_path", record.findings)
        self.assertEqual(len(record.actions), 1)
        action = record.actions[0]
        self.assertEqual(action.action, "restore_backup_path")
        self.assertIn("T", action.detail)

        # Cannot be made ready until the remediation action is confirmed.
        with self.assertRaises(ValueError):
            mark_ready(record, NOW)

        with self.assertRaises(ValueError):
            confirm_action(record, "unknown_action", NOW)

        confirmed = confirm_action(record, "restore_backup_path", NOW + timedelta(hours=1))
        self.assertTrue(confirmed.actions[0].confirmed)
        self.assertEqual(confirmed.revision, 1)
        with self.assertRaises(ValueError):
            mark_ready(record, NOW)  # the original record is untouched
        ready = mark_ready(confirmed, NOW + timedelta(hours=2))
        self.assertEqual(ready.status, "ready")

    def test_pump_available_but_still_in_min_rest_is_conditional(self):
        nodes, mains = loop_network()
        # Two pumps: one running at 40 l/s, one stopped at 30 l/s that needs
        # a 6 hour minimum rest but has only rested 1 hour; the fire starts
        # in 2 hours, so 40 < 50 required but 70 installed capacity suffices.
        pumps = [FirePump("p1", 40.0), FirePump("p2", 30.0, min_rest_hours=6.0)]
        states = {
            "p1": PumpState(running=True),
            "p2": PumpState(running=False, since_stop_hours=1.0),
        }
        fp = FireFlowPlan("fp-1", "S", "T", NOW + timedelta(hours=2), 2.0, 50.0)
        record = assure(fp, nodes, mains, pumps, states, reservoir(), [], NOW)

        self.assertEqual(record.status, "conditional")
        self.assertIn("pump_min_rest", record.findings)
        self.assertEqual(record.available_pump_flow_l_s, 40.0)
        action = record.actions[0]
        self.assertEqual(action.action, "wait_min_rest")
        self.assertIn("p2", action.detail)
        self.assertIn((NOW + timedelta(hours=5.0)).isoformat(), action.detail)
        with self.assertRaises(ValueError):
            mark_ready(record, NOW)

    def test_pump_rest_satisfied_before_fire_start_passes(self):
        nodes, mains = loop_network()
        pumps = [FirePump("p1", 30.0, min_rest_hours=6.0), FirePump("p2", 30.0)]
        states = {
            # 30 hours lead time comfortably clears the 6 hour rest.
            "p1": PumpState(running=False, since_stop_hours=0.0),
            "p2": PumpState(running=False, since_stop_hours=10.0),
        }
        record = assure(plan(required=50.0), nodes, mains, pumps, states, reservoir(), [], NOW)
        self.assertEqual(record.status, "pass")
        self.assertEqual(record.available_pump_flow_l_s, 60.0)

    def test_failure_when_no_path_at_fire_start(self):
        nodes, mains = radial_network()
        pumps = [FirePump("p1", 80.0)]
        outages = [MainOutage("m2", NOW - timedelta(hours=1), FIRE_START + timedelta(hours=3))]
        record = assure(plan(), nodes, mains, pumps, {"p1": PumpState(True)}, reservoir(), outages, NOW)

        self.assertEqual(record.status, "fail")
        self.assertIn("no_feed_path", record.findings)
        self.assertEqual(record.actions[0].action, "restore_path")
        with self.assertRaises(ValueError):
            mark_ready(record, NOW)

    def test_failure_when_installed_capacity_below_requirement(self):
        nodes, mains = loop_network()
        pumps = [FirePump("p1", 20.0)]
        record = assure(plan(required=50.0), nodes, mains, pumps, {"p1": PumpState(True)}, reservoir(), [], NOW)
        self.assertEqual(record.status, "fail")
        self.assertIn("insufficient_pump_capacity", record.findings)

    def test_failure_when_reservoir_below_protected_volume(self):
        nodes, mains = loop_network()
        pumps = [FirePump("p1", 80.0)]
        tanks = reservoir(current=100_000.0, protected=200_000.0)
        record = assure(plan(), nodes, mains, pumps, {"p1": PumpState(True)}, tanks, [], NOW)
        self.assertEqual(record.status, "fail")
        self.assertIn("reservoir_below_protected", record.findings)

    def test_snapshot_is_frozen_and_survives_topology_changes(self):
        nodes, mains = radial_network()
        pumps = [FirePump("p1", 80.0)]
        record = assure(plan(), nodes, mains, pumps, {"p1": PumpState(True)}, reservoir(), [], NOW)

        snapshot_before = record.snapshot
        self.assertIsInstance(snapshot_before, NetworkSnapshot)

        # Later topology change removes the backup-relevant structure and the
        # caller's live lists are mutated; the stored conclusion must not move.
        mains.clear()
        nodes.clear()
        self.assertEqual(tuple(m.main_id for m in record.snapshot.mains), ("m1", "m2"))
        self.assertEqual(record.status, "conditional")
        self.assertEqual(tuple(record.primary_path), ("m1", "m2"))
        self.assertEqual(record.snapshot, snapshot_before)

    def test_snapshot_re_evaluation_matches_historical_record(self):
        nodes, mains = radial_network()
        pumps = [FirePump("p1", 80.0)]
        first = assure(plan(), nodes, mains, pumps, {"p1": PumpState(True)}, reservoir(), [], NOW, topology_revision=1)

        # Network evolves before fire start; re-evaluating the *saved* snapshot
        # reproduces the original verdict instead of rewriting history.
        from aquaflow.network.fire_flow import evaluate

        again = evaluate(first.snapshot, plan())
        self.assertEqual(again.status, first.status)
        self.assertEqual(again.findings, first.findings)
        self.assertEqual(again.primary_path, first.primary_path)
        self.assertEqual(again.snapshot.topology_revision, 1)

    def test_scheduled_restoration_before_fire_start_is_conditional(self):
        nodes, mains = radial_network()
        pumps = [FirePump("p1", 80.0)]
        outages = [MainOutage("m1", NOW - timedelta(hours=1), NOW + timedelta(hours=6))]
        record = assure(plan(), nodes, mains, pumps, {"p1": PumpState(True)}, reservoir(), outages, NOW)
        self.assertEqual(record.status, "conditional")
        self.assertIn("primary_path_restoration_pending", record.findings)
        self.assertEqual(record.actions[0].action, "confirm_restoration")
        self.assertIn("m1", record.actions[0].detail)

    def test_service_facade_and_workflow_payload(self):
        service = AquaFlowService()
        nodes, mains = radial_network()
        pumps = [FirePump("p1", 80.0)]
        record = service.fire_flow_assurance(
            plan(), nodes, mains, pumps, {"p1": PumpState(True)}, reservoir(), [], NOW
        )
        self.assertEqual(record.status, "conditional")

        payload = {
            "plan": {
                "plan_id": "fp-1", "source": "S", "target": "T",
                "fire_start": FIRE_START.isoformat(), "duration_hours": 2.0,
                "required_flow_l_s": 50.0,
            },
            "nodes": [{"id": "S", "source": True}, {"id": "A"}, {"id": "T"}],
            "mains": [
                {"id": "m1", "a": "S", "b": "A", "flow_l_s": 100.0},
                {"id": "m2", "a": "A", "b": "T", "flow_l_s": 100.0},
            ],
            "pumps": [{"id": "p1", "flow_l_s": 80.0}],
            "pump_states": {"p1": {"running": True}},
            "reservoirs": [{"id": "r1", "current_volume_l": 2_000_000.0, "protected_volume_l": 200_000.0}],
            "outages": [],
            "now": NOW.isoformat(),
            "topology_revision": 3,
        }
        result = service.run_workflow("fire_flow_assurance", payload)
        self.assertEqual(result["decision"], "conditional")
        self.assertEqual(result["value"]["snapshot_revision"], 3)
        self.assertEqual(result["value"]["actions"][0]["action"], "restore_backup_path")
        self.assertEqual(result["workflow"], "fire_flow_assurance")


if __name__ == "__main__":
    unittest.main()
