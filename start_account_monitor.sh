#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="account_monitor_app.py"
BOOTSTRAP_SCRIPT="bootstrap_minute_snapshots.py"
SNAPSHOT_DIR="${SCRIPT_DIR}/minute_snapshots"
PYTHON_BIN="${PYTHON_BIN:-$HOME/.venv/bin/python}"
SERVICE_NAME="${MONITOR_SERVICE_NAME:-account_monitor}"
APP_PORT="${MONITOR_NAV_PORT:-7007}"
APP_HOST="${MONITOR_NAV_HOST:-127.0.0.1}"
LOG_DIR="${SCRIPT_DIR}/nv_logs"
LOG_FILE="${LOG_DIR}/${SERVICE_NAME}_$(date +'%Y%m%d_%H%M%S').log"
PID_FILE="${LOG_DIR}/${SERVICE_NAME}.pid"

mkdir -p "${LOG_DIR}"

if [ ! -f "${SCRIPT_DIR}/${PY_SCRIPT}" ]; then
    echo "Error: Python script not found at ${SCRIPT_DIR}/${PY_SCRIPT}"
    exit 1
fi

if [ ! -f "${SCRIPT_DIR}/${BOOTSTRAP_SCRIPT}" ]; then
    echo "Error: Bootstrap script not found at ${SCRIPT_DIR}/${BOOTSTRAP_SCRIPT}"
    exit 1
fi

if [ -f "${PID_FILE}" ]; then
    PID=$(cat "${PID_FILE}")
    if ps -p "${PID}" > /dev/null; then
        echo "Account monitor is already running with PID ${PID}; skipping start."
        exit 0
    fi
    echo "Warning: Stale PID file found. Removing..."
    rm -f "${PID_FILE}"
fi

if [ ! -d "${SNAPSHOT_DIR}" ] || ! compgen -G "${SNAPSHOT_DIR}/*_minute_snapshot.csv" > /dev/null; then
    echo "Minute snapshot directory is missing or empty. Running bootstrap..."
    if ! "${PYTHON_BIN}" -u -W ignore "${SCRIPT_DIR}/${BOOTSTRAP_SCRIPT}"; then
        echo "Error: Bootstrap script failed."
        exit 1
    fi
else
    echo "Minute snapshots already exist. Skipping bootstrap."
fi

RUNTIME_LOG_LEVEL="${RUNTIME_LOG_LEVEL:-ERROR}" MONITOR_NAV_HOST="${APP_HOST}" MONITOR_NAV_PORT="${APP_PORT}" nohup "${PYTHON_BIN}" -u -W ignore "${SCRIPT_DIR}/${PY_SCRIPT}" > "${LOG_FILE}" 2>&1 &
echo $! > "${PID_FILE}"

echo "Account monitor started."
echo "PID: $(cat "${PID_FILE}")"
echo "Host: ${APP_HOST}"
echo "Port: ${APP_PORT}"
echo "Log file: ${LOG_FILE}"
