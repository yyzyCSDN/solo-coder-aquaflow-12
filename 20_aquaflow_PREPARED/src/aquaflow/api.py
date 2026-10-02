from .treatment.chlorine_ct import compliant
from .network.isolation import isolated
from .network.fire_flow import assure as assure_fire_flow, confirm_action, mark_ready
from .meters.balance import balance
from .leak.localize import rank
from .workflow_service import WorkflowService
class AquaFlowService:
    def __init__(self):self.workflows=WorkflowService()
    def health(self):return {"service":"aquaflow","status":"ok","runtime_dependencies":0}
    def ct(self,rows,required):return compliant(rows,required)
    def isolate(self,nodes,pipes,closed,sources):return isolated(nodes,pipes,closed,sources)
    def water_balance(self,ins,outs,known_loss):return balance(ins,outs,known_loss)
    def leak_rank(self,rows):return rank(rows)
    def fire_flow_assurance(self,plan,nodes,mains,pumps,pump_states,reservoirs,outages,now,topology_revision=None):
        return assure_fire_flow(plan,nodes,mains,pumps,pump_states,reservoirs,outages,now,topology_revision)
    def fire_flow_confirm(self,record,action,at):return confirm_action(record,action,at)
    def fire_flow_ready(self,record,at):return mark_ready(record,at)
    def run_workflow(self,name,payload,received_at=None):return self.workflows.run(name,payload,received_at)
