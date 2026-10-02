# AquaFlow
Dependency-free municipal drinking-water treatment and distribution operations platform.

## Planned fire-flow assurance

`aquaflow.fireflow` evaluates a planned fire demand at a site and produces an
immutable assurance record:

* **passed** — the critical network path (plus an edge-disjoint backup route),
  the pump group (honouring minimum rest intervals), reservoir protected
  storage and current outage state all support the demand; the record is
  immediately ready.
* **conditional** — feasible after precise remediation actions
  (`provide_backup_path`, `wait_pump_min_rest`, `confirm_outage_restoration`).
  The record cannot become ready until every action is confirmed.
* **failed** — a hard deficiency (no route, undersized route, active outage at
  the planned start, insufficient pump capacity even after rest intervals, or
  protected-volume breach) with no remediation shortcut.

Each record embeds a frozen `NetworkSnapshot` captured at evaluation time.
Replaying a stored snapshot always reproduces the historical outcome, so later
topology changes never rewrite past conclusions.

```python
from aquaflow.api import AquaFlowService

svc = AquaFlowService()
record = svc.fire_flow_plan(site_id, required_flow_l_s, duration_s, starts_at,
                            nodes, mains, pumps, reservoirs, outages, now)
svc.fire_flow_confirm(record.record_id, action.code, action.ref, confirmed_at)
svc.fire_flow_replay(record.record_id)
```

The same capability is available as the `fire_flow_assurance` workflow via
`AquaFlowService.run_workflow`.
