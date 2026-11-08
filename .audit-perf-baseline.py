import datetime as dt, json, os, pathlib, statistics, tempfile, time, tracemalloc
from analytics import lab_simulator
from services import lab_tasks, agentic_analytics
from database.connection import init_db, get_session, get_engine
from database.models import MetricSample
from database import repository
N=5
def measure(fn, repeats=N):
    times=[]; peaks=[]; result=None
    for _ in range(repeats):
        tracemalloc.start(); t=time.perf_counter(); result=fn(); times.append(time.perf_counter()-t)
        peaks.append(tracemalloc.get_traced_memory()[1]); tracemalloc.stop()
    return {'median_s':round(statistics.median(times),6),'min_s':round(min(times),6),'max_s':round(max(times),6),'median_python_peak_mb':round(statistics.median(peaks)/1024/1024,3)},result
os.environ['DATABASE_URL']='sqlite:///'+str(pathlib.Path(tempfile.gettempdir())/'netpilot-perf-benchmark.sqlite')
cfg=dict(devices=tuple(f'R{i}' for i in range(8)),duration_hours=15,interval_seconds=180,seed=321,start=dt.datetime(2025,1,1),metrics=lab_simulator.DEFAULT_METRICS,events={})
gen,dataset=measure(lambda:lab_simulator.simulate(**cfg),repeats=3)
refs=dataset.series_refs(); selected=[r for r in refs if r.device=='R0']; ref=next(r for r in selected if r.metric=='ping.avg_rtt_ms')
inspect=measure(lambda:agentic_analytics._inspect(dataset))
stats=measure(lambda:lab_tasks.run_statistics(dataset,selected))
anomaly=measure(lambda:lab_tasks.run_anomaly_comparison(dataset,[ref],detectors=('rolling_mad','ewma'),sweep_curve=False))
forecast=measure(lambda:lab_tasks.run_forecast_comparison(dataset,[ref],horizon=12,models=('naive','seasonal_naive','holt_damped'),ensemble=False),repeats=3)
init_db(); engine=get_engine()
with engine.begin() as conn:
 conn.execute(MetricSample.__table__.insert(),[dict(device_name='R0',interface_name='eth0',metric='ping.avg_rtt_ms',value=float(i%100),unit='ms',data_source='live',collected_at=dt.datetime(2025,1,1)+dt.timedelta(seconds=i)) for i in range(20000)])
session=get_session()
try: query=measure(lambda:repository.get_metric_series(session,'R0','ping.avg_rtt_ms',data_source='live'),repeats=5)
finally: session.close()
try:
 import psutil; rss=round(psutil.Process().memory_info().rss/1024/1024,2)
except Exception: rss=None
print(json.dumps({'dataset':{'rows':len(dataset.frame),'series':len(refs),'seed':321,'devices':8,'points_per_series':300},'generation':gen,'inspection':inspect,'statistics_all_R0_metrics':stats[0],'anomaly_two_detectors_one_series':anomaly[0],'forecast_three_models_one_series':forecast[0],'metric_history_query_20000_rows':query[0],'rss_mb_after_bench':rss,'correctness':{'dataset_source':dataset.source_type,'stats_status':stats[1].status,'anomaly_status':anomaly[1].status,'forecast_status':forecast[1].status,'forecast_eval_rows':len(forecast[1].table)}},default=str,indent=2))
