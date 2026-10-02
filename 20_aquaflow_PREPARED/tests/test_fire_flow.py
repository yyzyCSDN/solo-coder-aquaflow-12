import unittest
from datetime import datetime, timedelta, timezone

from aquaflow.api import AquaFlowService
from aquaflow.network.pathing import Junction, Main
from aquaflow.fireflow.assurance import (
    FirePump,
    Outage,
    ProtectedReservoir,
    replay_snapshot,
)

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=24)
REQUIRED = 90.0
DURATION = 7200.0  # 2 hours


def big_main(mid, a, b):
    # 300 mm mains comfortably carry the required fire flow.
    return Main(mid, a, b, length_m=500.0, diameter_mm=300.0)


def small_main(mid, a, b):
    return Main(mid, a, b, length_m=500.0, diameter_mm=100.0)


def reservoirs_ok():
    # 1 ML drawdown above the protected level plus inflow covers 90 L/s * 2h.
    return [ProtectedReservoir("R1", available_volume_l=1_200_000.0,
                               protected_volume_l=200_000.0, inflow_l_s=10.0)]


def pump_group(resting=False):
    if resting:
        return [FirePump("P1", flow_l_s=100.0, available=True, running=False,
                         since_stop=30.0, min_rest=120.0)]
    return [FirePump("P1", flow_l_s=100.0, available=True, running=True,
                     since_stop=0.0, min_rest=120.0)]


def looped_network():
    nodes = [
        Junction("S1", elevation_m=10.0, demand_l_s=0.0, source=True),
        Junction("A", elevation_m=12.0, demand_l_s=5.0),
        Junction("B", elevation_m=14.0, demand_l_s=5.0),
        Junction("FIRE", elevation_m=16.0, demand_l_s=5.0, critical=True),
    ]
    mains = [
        big_main("M1", "S1", "A"),
        big_main("M2", "A", "FIRE"),
        big_main("M3", "S1", "B"),
        big_main("M4", "B", "FIRE"),
    ]
    return nodes, mains


def single_path_network():
    nodes = [
        Junction("S1", elevation_m=10.0, demand_l_s=0.0, source=True),
        Junction("A", elevation_m=12.0, demand_l_s=5.0),
        Junction("FIRE", elevation_m=16.0, demand_l_s=5.0, critical=True),
    ]
    mains = [big_main("M1", "S1", "A"), big_main("M2", "A", "FIRE")]
    return nodes, mains


class FireFlowAssuranceTests(unittest.TestCase):
    def test_passed_when_paths_pumps_storage_all_sufficient(self):
        svc = AquaFlowService()
        nodes, mains = looped_network()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs_ok(), now=NOW, record_id="FF-1",
        )
        self.assertEqual(record.outcome, "passed")
        self.assertTrue(record.ready)
        self.assertEqual(record.reasons, ())
        self.assertEqual(record.actions, ())
        self.assertGreaterEqual(record.metrics["primary_capacity_l_s"], REQUIRED)
        self.assertIn("backup_path", record.metrics)

    def test_conditional_when_backup_path_missing(self):
        svc = AquaFlowService()
        nodes, mains = single_path_network()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs_ok(), now=NOW, record_id="FF-2",
        )
        self.assertEqual(record.outcome, "conditional")
        self.assertFalse(record.ready)
        codes = [(a.code, a.ref) for a in record.actions]
        self.assertEqual(codes, [("provide_backup_path", "FIRE")])
        # The remediation must be precise about what to build/open.
        self.assertIn("edge-disjoint backup feed", record.actions[0].detail)
        self.assertIn("M1-M2", record.actions[0].detail)

    def test_conditional_not_ready_until_action_confirmed(self):
        svc = AquaFlowService()
        nodes, mains = single_path_network()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs_ok(), now=NOW, record_id="FF-3",
        )
        self.assertFalse(record.ready)
        with self.assertRaises(ValueError):
            svc.fire_flow_confirm("FF-3", "provide_backup_path", "WRONG-SITE", NOW)
        confirmed = svc.fire_flow_confirm(
            "FF-3", "provide_backup_path", "FIRE", NOW + timedelta(hours=1))
        self.assertTrue(confirmed.ready)
        self.assertEqual(confirmed.outcome, "conditional")
        self.assertEqual(confirmed.version, 2)
        self.assertEqual(set(confirmed.confirmed_actions), {"provide_backup_path:FIRE"})

    def test_pump_available_but_within_minimum_rest_interval(self):
        svc = AquaFlowService()
        nodes, mains = looped_network()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(resting=True), reservoirs_ok(), now=NOW, record_id="FF-4",
        )
        # Capacity exists once the rest interval clears, so it is conditional.
        self.assertEqual(record.outcome, "conditional")
        self.assertFalse(record.ready)
        action = record.actions[0]
        self.assertEqual(action.code, "wait_pump_min_rest")
        self.assertEqual(action.ref, "P1")
        self.assertIn("minimum rest interval", action.detail)
        self.assertEqual(record.metrics["pumps_in_min_rest"], ["P1"])
        # Confirming the wait clears readiness.
        confirmed = svc.fire_flow_confirm(
            "FF-4", "wait_pump_min_rest", "P1", NOW + timedelta(hours=2))
        self.assertTrue(confirmed.ready)

    def test_pump_rest_that_cannot_close_the_gap_fails(self):
        svc = AquaFlowService()
        nodes, mains = looped_network()
        pumps = [FirePump("P1", flow_l_s=40.0, available=True, running=False,
                          since_stop=30.0, min_rest=120.0)]
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pumps, reservoirs_ok(), now=NOW, record_id="FF-5",
        )
        self.assertEqual(record.outcome, "failed")
        self.assertIn("insufficient_pump_capacity", record.reasons)
        # Failures carry no remediation shortcuts and can never become ready.
        self.assertEqual(record.actions, ())
        self.assertFalse(record.ready)
        with self.assertRaises(ValueError):
            svc.fire_flow_confirm("FF-5", "wait_pump_min_rest", "P1", NOW)

    def test_protected_volume_breach_fails(self):
        svc = AquaFlowService()
        nodes, mains = looped_network()
        reservoirs = [ProtectedReservoir("R1", available_volume_l=210_000.0,
                                         protected_volume_l=200_000.0, inflow_l_s=0.0)]
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs, now=NOW, record_id="FF-6",
        )
        self.assertEqual(record.outcome, "failed")
        self.assertIn("protected_volume_breach", record.reasons)
        self.assertGreater(record.metrics["reservoir_storage_deficit_l"], 0.0)

    def test_active_outage_on_sole_path_fails(self):
        svc = AquaFlowService()
        nodes, mains = single_path_network()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs_ok(),
            outages=[Outage("M1")], now=NOW, record_id="FF-7",
        )
        self.assertEqual(record.outcome, "failed")
        self.assertIn("active_outage:M1", record.reasons)

    def test_confirmed_restoration_before_start_is_conditional_until_verified(self):
        svc = AquaFlowService()
        nodes, mains = single_path_network()
        # Restoration promised for 2h before the event, but not yet confirmed now.
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs_ok(),
            outages=[Outage("M1", restored_at=START - timedelta(hours=2))],
            now=NOW, record_id="FF-8",
        )
        # Route exists at start, but field confirmation plus backup are pending.
        self.assertEqual(record.outcome, "conditional")
        codes = {a.code for a in record.actions}
        self.assertIn("confirm_outage_restoration", codes)
        self.assertIn("provide_backup_path", codes)
        self.assertFalse(record.ready)
        for action in record.actions:
            record = svc.fire_flow.confirm_action(
                "FF-8", action.code, action.ref, NOW + timedelta(hours=1))
        self.assertTrue(record.ready)

    def test_snapshot_is_immutable_against_later_topology_changes(self):
        svc = AquaFlowService()
        nodes, mains = looped_network()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pump_group(), reservoirs_ok(), now=NOW, record_id="FF-9",
        )
        snapshot = record.snapshot

        # Later topology change: shrink every main, ground the pump and drain
        # the reservoir. These are new objects; the snapshot keeps its own.
        later = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START + timedelta(days=1),
            nodes,
            [Main(m.main_id, m.a, m.b, m.length_m, 100.0, open=m.open) for m in mains],
            [FirePump("P1", 5.0, False, False, 0.0, 999.0)],
            [ProtectedReservoir("R1", 0.0, 0.0, 0.0)],
            now=START, record_id="FF-10",
        )
        self.assertEqual(later.outcome, "failed")

        # The historical record and its replay stay "passed".
        self.assertEqual(svc.fire_flow_replay("FF-9").outcome, "passed")
        replayed = replay_snapshot(snapshot)
        self.assertEqual(replayed.outcome, "passed")
        self.assertEqual(replayed.reasons, ())
        self.assertEqual(len(snapshot.mains), 4)
        self.assertTrue(all(m.open for m in snapshot.mains))

    def test_mutating_input_lists_after_planning_does_not_change_history(self):
        svc = AquaFlowService()
        nodes, mains = looped_network()
        pumps = pump_group()
        reservoirs = reservoirs_ok()
        record = svc.fire_flow_plan(
            "FIRE", REQUIRED, DURATION, START, nodes, mains,
            pumps, reservoirs, now=NOW, record_id="FF-11",
        )
        # Caller mutates their collections after the fact.
        mains[0] = small_main("M1", "S1", "A")
        pumps.append(FirePump("P2", 0.0, False, False, 0.0, 0.0))
        reservoirs.clear()
        self.assertEqual(record.snapshot.mains[0].diameter_mm, 300.0)
        self.assertEqual(len(record.snapshot.pumps), 1)
        self.assertEqual(len(record.snapshot.reservoirs), 1)
        self.assertEqual(svc.fire_flow_replay("FF-11").outcome, "passed")

    def test_workflow_payload_returns_decision_and_snapshot(self):
        svc = AquaFlowService()
        nodes, mains = single_path_network()
        payload = {
            "site": "FIRE",
            "required_flow_l_s": 90.0,
            "duration_s": 7200.0,
            "now": NOW.isoformat(),
            "starts_at": START.isoformat(),
            "nodes": [{"id": n.node_id, "elevation_m": n.elevation_m,
                       "source": n.source, "critical": n.critical} for n in nodes],
            "mains": [{"id": m.main_id, "a": m.a, "b": m.b,
                       "length_m": m.length_m, "diameter_mm": m.diameter_mm} for m in mains],
            "pumps": [{"id": "P1", "flow_l_s": 100.0, "running": True, "min_rest": 120.0}],
            "reservoirs": [{"id": "R1", "available_volume_l": 1_200_000.0,
                            "protected_volume_l": 200_000.0, "inflow_l_s": 10.0}],
            "outages": [],
        }
        result = svc.run_workflow("fire_flow_assurance", payload, received_at=NOW)
        self.assertEqual(result["decision"], "remediate")
        self.assertEqual(result["value"], "conditional")
        self.assertFalse(result["evidence"]["ready"])
        self.assertEqual(result["evidence"]["actions"][0]["code"], "provide_backup_path")
        self.assertEqual(result["evidence"]["snapshot"]["site_id"], "FIRE")
        self.assertEqual(result["workflow"], "fire_flow_assurance")


if __name__ == "__main__":
    unittest.main()
