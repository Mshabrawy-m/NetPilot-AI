"""One-off Phase 2 evaluation over the stored DEMO dataset (clearly
synthetic!). Copies data/netpilot_demo.db to a throwaway file first so
this never mutates the user's demo database.

Prints: available metrics, detection outcome on a chosen series, and a
full forecast comparison (chronological split, MAE/RMSE per model on
identical held-out timestamps, persisted runs) - all as concrete numbers
for the delivery report. These results characterise the SYNTHETIC demo
fixtures only and are NOT evidence of real-network performance.

Run:  .venv/Scripts/python tools/phase2_demo_eval.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

src = ROOT / "data" / "netpilot_demo.db"
tmp = ROOT / "data" / "_phase2_demo_eval.db"
if not src.exists():
    print(f"MISSING {src} - start the app once in demo mode first.")
    raise SystemExit(1)
# WAL-aware consistent copy (never touches the original file)
for _p in (tmp, tmp.with_name(tmp.name + "-shm"),
           tmp.with_name(tmp.name + "-wal")):
    _p.unlink(missing_ok=True)
scon = sqlite3.connect(src)
tcon = sqlite3.connect(tmp)
try:
    scon.backup(tcon)
finally:
    tcon.close()
    scon.close()

import os
os.environ["NETPILOT_DEMO_MODE"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{tmp.as_posix()}"

from database import repository                       # noqa: E402
from database.connection import get_session, init_db  # noqa: E402
from services import anomaly_service, forecast_service  # noqa: E402

init_db()
session = get_session()
try:
    avail = repository.get_available_metrics(session, non_null_only=True)
    demo = [(d, m, u) for (d, m, u, s) in avail if s == "demo" and m]
    print("Demo metrics (device, metric, unit):")
    for row in sorted(set(demo)):
        print("  ", row)

    # choose the metric with the most stored non-null samples
    best = None
    for d, m, u in sorted(set(demo)):
        n = len(repository.get_metric_series(session, d, m,
                                             data_source="demo"))
        if best is None or n > best[0]:
            best = (n, d, m, u)
    n, dev, met, unit = best
    print(f"\nSelected series: device={dev} metric={met} unit={unit} "
          f"rows={n} (SYNTHETIC demo data)")

    print("\n--- anomaly detection across all demo device-level series "
          "(rolling MAD + z-score baselines) ---")
    det_examples = []
    for d, m in sorted({(dd, mm) for (dd, mm, _uu) in demo}):
        # detection always runs on ONE well-defined series: per interface
        # when the metric is interface-scoped, else the device-level rows
        ifaces = sorted({r.interface_name for r in repository.get_metric_series(
            session, d, m, data_source="demo") if r.interface_name})
        scopes = ([(i, False) for i in ifaces] if ifaces
                  else [(None, True)])
        for iface, dev_only in scopes:
            det = anomaly_service.run_detection(
                session, device_name=d, metric=m, interface_name=iface,
                data_source="demo", device_level_only=dev_only)
            label = f"{d}/{m}" + (f"@{iface}" if iface else "")
            if det["status"] != "ok":
                print(f"  {label:<34} {det['status']}: "
                      f"{'; '.join(det['reasons'])[:70]}")
                continue
            pp = det["persisted"]
            print(f"  {label:<34} points={det['n_anomalies']:>2} "
                  f"created={pp['created']} updated={pp['updated']} "
                  f"resolved={det['resolved']}")
            if det["points"] and len(det_examples) < 3:
                det_examples.append((label, det["points"][0]))
    for label, p in det_examples:
        print(f"  example point on {label}: {p.timestamp} "
              f"obs={p.observed:.2f} "
              f"baseline={None if p.baseline is None else round(p.baseline, 2)} "
              f"[{p.detector}/{p.severity}] {p.explanation[:90]}")

    sel_ifaces = sorted({r.interface_name for r in repository.get_metric_series(
        session, dev, met, data_source="demo") if r.interface_name})
    res = forecast_service.run_forecast_comparison(
        session, device_name=dev, metric=met,
        interface_name=(sel_ifaces[0] if sel_ifaces else None),
        data_source="demo", horizon=10,
        device_level_only=not sel_ifaces)
    print("\n--- forecast comparison (identical held-out timestamps) ---")
    print("status:", res["status"])
    if res["status"] == "ok":
        sp = res["split"]
        print(f"split: train={len(sp.train)} val={len(sp.val)} "
              f"test={len(sp.test)}  "
              f"train_end={sp.train_end} test_end={sp.test_end}")
        for r in res["runs"]:
            if r.get("status") == "ok":
                print(f"  {r['model']:<14} MAE={r['mae']:.4g} "
                      f"RMSE={r['rmse']:.4g} "
                      f"R2={('n/a' if r['r2'] is None else round(r['r2'], 3))} "
                      f"n_eval={r['n_eval']}")
            else:
                print(f"  {r['model']:<14} {r.get('status')}: "
                      f"{r.get('reason', '')[:80]}")
        runs = repository.get_forecast_runs(session, data_source="demo")
        print(f"persisted forecast_runs rows: {len(runs)} "
              f"(statuses: {sorted({r.status for r in runs})})")
        ok_run = next((r for r in runs if r.status == "ok"), None)
        if ok_run is not None:
            preds = repository.get_forecast_predictions(session, ok_run.id)
            print(f"persisted predictions for latest ok run "
                  f"#{ok_run.id} ({ok_run.model_name}): {len(preds)}")
finally:
    session.close()
    import time
    import database.connection as dbconn
    dbconn.get_engine().dispose()        # release WAL handles before unlink
    leftovers = []
    for p in (tmp, tmp.with_name(tmp.name + "-shm"),
              tmp.with_name(tmp.name + "-wal")):
        for _attempt in range(10):      # brief retries: Windows file handles
            try:
                p.unlink(missing_ok=True)
                break
            except PermissionError:
                time.sleep(0.3)
        if p.exists():
            leftovers.append(p.name)
    print("\n" + ("(throwaway demo copy removed; original demo DB "
                  "untouched)" if not leftovers else
                  f"(NOTE: {leftovers} still locked; it is removed at the "
                  "next run start - original demo DB untouched)"))

