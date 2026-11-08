import datetime as dt,json,os,pathlib,statistics,time,tracemalloc
from analytics import lab_simulator
from services import agentic_analytics
from database.connection import init_db,get_session,get_engine
from database import repository

def measure(fn,n=5):
 ts=[];mem=[];out=None
 for _ in range(n):
  tracemalloc.start();t=time.perf_counter();out=fn();ts.append(time.perf_counter()-t);mem.append(tracemalloc.get_traced_memory()[1]);tracemalloc.stop()
 return {'median_s':round(statistics.median(ts),6),'median_python_peak_mb':round(statistics.median(mem)/1048576,3)},out
os.environ['DATABASE_URL']='sqlite:///'+str(pathlib.Path('.audit-perf-agent.sqlite').resolve()).replace('\\','/')
init_db();dataset=lab_simulator.simulate(devices=('R0',),duration_hours=15,interval_seconds=180,seed=321,start=dt.datetime(2025,1,1),metrics=lab_simulator.DEFAULT_METRICS,events={})
payload={'objective':'Compute statistics','steps':[{'tool':'run_statistics','series_ids':['s0'],'horizon':10,'purpose':'Compute descriptive statistics'}]}
class Fake:
 def complete_json(self,_sys,_user,schema):return schema.model_validate(payload)
planner=Fake();planning,plan=measure(lambda:agentic_analytics.plan_tools(planner,'summarize latency',dataset),10)
def execute():
 s=get_session()
 try:return agentic_analytics.execute_plan(s,dataset,plan,request_text='summarize latency')
 finally:s.close()
tooling,result=measure(execute,3)
s=get_session()
try:history,rows=measure(lambda:agentic_analytics.list_runs(s,limit=20),5)
finally:s.close()
print(json.dumps({'dataset_rows':len(dataset.frame),'plan_steps':len(plan.steps),'fake_provider_plan_validation':planning,'agentic_execution_with_statistics_and_persistence':tooling,'agentic_history_retrieval':history,'history_rows':len(rows),'last_tool_status':result['steps'][-1]['status']},indent=2))
