#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${SCRIPT_DIR}/runtime_logs/account_monitor.pid"
START_SCRIPT="${SCRIPT_DIR}/start_account_monitor.sh"

process_is_running() {
    local pid="$1"
    local process_state

    process_state="$(ps -p "${pid}" -o stat= 2>/dev/null | tr -d '[:space:]')"
    [ -n "${process_state}" ] && [[ "${process_state}" != Z* ]]
}

wait_for_exit() {
    local pid="$1"
    local timeout_seconds="$2"

    for ((second = 0; second < timeout_seconds; second++)); do
        if ! process_is_running "${pid}"; then
            return 0
        fi
        sleep 1
    done
    ! process_is_running "${pid}"
}

if [ ! -f "${START_SCRIPT}" ]; then
    echo "Error: Start script not found at ${START_SCRIPT}"
    exit 1
fi

if [ -f "${PID_FILE}" ]; then
    PID="$(tr -d '[:space:]' < "${PID_FILE}")"
    if ! [[ "${PID}" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: Invalid PID in ${PID_FILE}: ${PID}"
        exit 1
    fi

    if process_is_running "${PID}"; then
        PROCESS_COMMAND="$(ps -p "${PID}" -o args= 2>/dev/null || true)"
        if [[ "${PROCESS_COMMAND}" != *"account_monitor_app.py"* ]]; then
            echo "Error: PID ${PID} does not belong to account_monitor_app.py; refusing to kill it."
            exit 1
        fi

        echo "Stopping account monitor with PID ${PID}..."
        kill "${PID}" 2>/dev/null || true

        if ! wait_for_exit "${PID}" 10; then
            echo "Account monitor did not stop within 10 seconds; forcing shutdown..."
            kill -KILL "${PID}" 2>/dev/null || true
            if ! wait_for_exit "${PID}" 10; then
                echo "Error: Account monitor process ${PID} is still running; restart aborted."
                exit 1
            fi
        fi
        echo "Account monitor stopped."
    else
        echo "PID ${PID} is not running; start script will replace the stale PID file."
    fi
else
    echo "PID file not found; starting account monitor directly."
fi

echo "Starting account monitor..."
bash "${START_SCRIPT}"
