# NetPilot AI - one image, two services (Slice 8).
# The same artifact runs either the Streamlit dashboard (default) or the
# opt-in read-only FastAPI/WS API, selected by NETPILOT_SERVICE. Storage is
# whatever DATABASE_URL points at (SQLite file inside the container by
# default, or PostgreSQL via compose - psycopg2-binary is installed below).
#
# NOTE: this image analyses and presents lab data; the Containerlab lab
# itself must run on a Docker host (see docker-compose.yml 'lab' profile).
FROM python:3.12-slim-bookworm

# No core dumps, no pip cache, bytecode off (image stays lean).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# python:slim images ship without the ping/traceroute binaries, which the
# connectivity probes and the demo loopback self-test execute. The code also
# has a bounded ping-TTL traceroute fallback, but installing the native tool
# gives a proper multi-probe path. Streamlit Cloud gets the same packages
# through the root packages.txt file.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iputils-ping traceroute \
 && rm -rf /var/lib/apt/lists/*

# Dependencies first so the layer cache survives source edits.
# The PostgreSQL driver is declared ONCE in requirements.txt (psycopg2-binary)
# and installed from there - no second, potentially conflicting pin here.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
# Defensive: strip CRLF/BOM if a Windows checkout smuggled them into the
# script (bash dies on '\r'), and make it executable.
RUN sed -i -e 's/\r$//' -e '1s/^\xEF\xBB\xBF//' /usr/local/bin/entrypoint.sh \
 && chmod +x /usr/local/bin/entrypoint.sh

# Run as a non-root user; /data holds the SQLite file when DATABASE_URL is
# left unset (mount a volume here to persist it across container updates).
RUN useradd --create-home --shell /usr/sbin/nologin netpilot \
 && mkdir -p /data \
 && chown -R netpilot:netpilot /app /data
USER netpilot

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
EXPOSE 8501 8000

# Default command is overridden by the entrypoint via NETPILOT_SERVICE;
# kept so `docker run <image>` without compose starts the dashboard.
CMD ["dashboard"]

# Entrypoint lives in docker/entrypoint.sh (bash, not the truncated sh of
# the slim image) and execs streamlit or uvicorn based on $NETPILOT_SERVICE.
