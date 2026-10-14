#!/usr/bin/env bash
# NetPilot AI container entrypoint (Slice 8).
# One image, two roles: NETPILOT_SERVICE=dashboard (default) runs the
# Streamlit UI on 8501; =api runs the read-only FastAPI/WS API on 8000.
# Anything else is passed through so `docker run <image> pytest` still works.
set -euo pipefail

cd /app

case "${1:-${NETPILOT_SERVICE:-dashboard}}" in
  dashboard)
    exec streamlit run app.py \
      --server.port="${NETPILOT_DASHBOARD_PORT:-8501}" \
      --server.address=0.0.0.0 \
      --server.headless=true
    ;;
  api)
    exec python -m uvicorn api.app:app \
      --host 0.0.0.0 --port "${NETPILOT_API_PORT:-8000}"
    ;;
  *)
    exec "$@"
    ;;
esac
