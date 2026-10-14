"""NetPilot AI Phase 2 - analytics layer.

Reusable, UI-independent analytics built on the Phase 1 stored measurements:

* ``time_series``     - query/preparation of metric series (chronological,
  deterministic de-duplication, optional labelled imputation/resampling)
* ``data_quality``    - per-series quality report + readiness gates
* ``derived_metrics`` - rates/deltas from cumulative counters with
  counter-reset and gap handling
* ``eda``             - summary statistics / distributions / correlations
* ``anomaly_detection`` - explainable rolling baselines (+ optional ML)
* ``forecasting``     - naive / seasonal-naive / statistical baselines
* ``evaluation``      - chronological splits, leakage checks, MAE/RMSE/R2

All timestamps are naive UTC (the Phase 1 storage convention). Nothing here
touches the network, executes commands, or mutates raw metric samples.
"""
