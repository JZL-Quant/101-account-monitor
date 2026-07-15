#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ID="$(date +'%Y%m%d_%H%M%S')"

PROMETHEUS_DIR=""
PROMETHEUS_DATA_DIR=""
PROMETHEUS_SERVICE="prometheus"
PROMETHEUS_URL="http://127.0.0.1:9090"
PYTHON_BIN="${PYTHON_BIN:-}"
PROMTOOL_BIN="${PROMTOOL_BIN:-}"
ASSUME_YES=0
BACKFILL_ARGS=()
START_COMMAND=()

usage() {
    cat <<'EOF'
用法（Prometheus 解压目录直接运行，推荐）：
  sudo bash ops/backfill_prometheus.sh \
    --prometheus-dir /home/ec2-user/prometheus-3.3.0-rc.0.linux-arm64 \
    --python-bin /home/ec2-user/.venv/bin/python \
    --start 2025-07-14T00:00:00Z --end 2026-07-14T20:00:00Z

脚本自动找到该目录内的 promtool 和 data，并完成生成、停服、备份、导入和重启。

参数：
  --prometheus-dir PATH        Prometheus 解压/运行目录（推荐）
  --prometheus-data-dir PATH   systemd 模式的数据目录
  --prometheus-service NAME    systemd 服务名，默认 prometheus
  --prometheus-url URL         默认 http://127.0.0.1:9090
  --python-bin PATH            项目虚拟环境 Python
  --promtool PATH              覆盖自动推导的 promtool 路径
  --yes                        跳过停服前确认
  -h, --help                   显示帮助

其他参数原样传给 ops.backfill_prometheus，例如 --account、--start、--end。
EOF
}

while (($#)); do
    case "$1" in
        --prometheus-dir) PROMETHEUS_DIR="${2:?缺少路径}"; shift 2 ;;
        --prometheus-data-dir) PROMETHEUS_DATA_DIR="${2:?缺少路径}"; shift 2 ;;
        --prometheus-service) PROMETHEUS_SERVICE="${2:?缺少名称}"; shift 2 ;;
        --prometheus-url)
            PROMETHEUS_URL="${2:?缺少 URL}"
            BACKFILL_ARGS+=("$1" "$2")
            shift 2
            ;;
        --python-bin) PYTHON_BIN="${2:?缺少路径}"; shift 2 ;;
        --promtool) PROMTOOL_BIN="${2:?缺少路径}"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) BACKFILL_ARGS+=("$1"); shift ;;
    esac
done

fail() { echo "ERROR: $*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || fail "需要 root 权限停服和写入 Prometheus 数据目录，请使用 sudo。"

if [[ -z "${PYTHON_BIN}" ]]; then
    CALLER_HOME=""
    [[ -n "${SUDO_USER:-}" ]] && CALLER_HOME="$(getent passwd "${SUDO_USER}" | cut -d: -f6)"
    for candidate in "${VIRTUAL_ENV:-}/bin/python" "${PROJECT_ROOT}/.venv/bin/python" "${CALLER_HOME}/.venv/bin/python"; do
        if [[ "${candidate}" != "/bin/python" && -x "${candidate}" ]]; then
            PYTHON_BIN="${candidate}"
            break
        fi
    done
fi
[[ -n "${PYTHON_BIN}" && -x "${PYTHON_BIN}" ]] || fail "找不到虚拟环境 Python，请使用 --python-bin 指定。"

MODE="systemd"
if [[ -n "${PROMETHEUS_DIR}" ]]; then
    MODE="standalone"
    PROMETHEUS_DIR="$(readlink -f "${PROMETHEUS_DIR}")"
    [[ -d "${PROMETHEUS_DIR}" && "${PROMETHEUS_DIR}" != "/" ]] || fail "Prometheus 目录无效: ${PROMETHEUS_DIR}"
    [[ -x "${PROMETHEUS_DIR}/prometheus" ]] || fail "找不到: ${PROMETHEUS_DIR}/prometheus"
    [[ -f "${PROMETHEUS_DIR}/prometheus.yml" ]] || fail "找不到: ${PROMETHEUS_DIR}/prometheus.yml"
    [[ -n "${PROMETHEUS_DATA_DIR}" ]] || PROMETHEUS_DATA_DIR="${PROMETHEUS_DIR}/data"
    [[ -n "${PROMTOOL_BIN}" ]] || PROMTOOL_BIN="${PROMETHEUS_DIR}/promtool"
else
    [[ -n "${PROMETHEUS_DATA_DIR}" ]] || { usage >&2; fail "请指定 --prometheus-dir（推荐）或 --prometheus-data-dir。"; }
    command -v systemctl >/dev/null 2>&1 || fail "systemd 模式需要 systemctl。"
    systemctl is-active --quiet "${PROMETHEUS_SERVICE}" || fail "服务 ${PROMETHEUS_SERVICE} 当前不是 active。"
fi

if [[ -z "${PROMTOOL_BIN}" ]]; then
    PROMTOOL_BIN="$(command -v promtool || true)"
fi
[[ -n "${PROMTOOL_BIN}" && -x "${PROMTOOL_BIN}" ]] || fail "找不到 promtool；使用 --prometheus-dir 后会自动使用该目录内的 promtool。"

PROMETHEUS_DATA_DIR="$(readlink -f "${PROMETHEUS_DATA_DIR}")"
[[ -d "${PROMETHEUS_DATA_DIR}" && "${PROMETHEUS_DATA_DIR}" != "/" ]] || fail "Prometheus 数据目录无效: ${PROMETHEUS_DATA_DIR}"

find_prometheus_pid() {
    local pid executable
    while read -r pid; do
        executable="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
        if [[ "${executable}" == "${PROMETHEUS_DIR}/prometheus" ]]; then
            printf '%s\n' "${pid}"
            return 0
        fi
    done < <(pgrep -f '[p]rometheus' || true)
    return 1
}

wait_ready() {
    local i
    for ((i=0; i<100; i++)); do
        curl -fsS "${PROMETHEUS_URL}/-/ready" >/dev/null 2>&1 && return 0
        sleep 0.2
    done
    return 1
}

start_prometheus() {
    if [[ "${MODE}" == "systemd" ]]; then
        systemctl start "${PROMETHEUS_SERVICE}"
    else
        (cd "${PROMETHEUS_DIR}" && setsid -f "${START_COMMAND[@]}" >> prometheus.log 2>&1)
        wait_ready
    fi
}

stop_prometheus() {
    if [[ "${MODE}" == "systemd" ]]; then
        systemctl stop "${PROMETHEUS_SERVICE}"
        return
    fi
    local pid="$1" i
    curl -fsS -X POST "${PROMETHEUS_URL}/-/quit" >/dev/null || fail "无法通过 /-/quit 停止 Prometheus，请确认启动参数包含 --web.enable-lifecycle。"
    for ((i=0; i<100; i++)); do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.2
    done
    fail "Prometheus 未在 20 秒内停止。"
}

PROM_PID=""
if [[ "${MODE}" == "standalone" ]]; then
    command -v curl >/dev/null 2>&1 || fail "缺少 curl。"
    command -v pgrep >/dev/null 2>&1 || fail "缺少 pgrep。"
    command -v setsid >/dev/null 2>&1 || fail "缺少 setsid。"
    PROM_PID="$(find_prometheus_pid || true)"
    [[ -n "${PROM_PID}" ]] || fail "未找到 ${PROMETHEUS_DIR}/prometheus 进程。"
    curl -fsS "${PROMETHEUS_URL}/-/ready" >/dev/null || fail "Prometheus 当前未就绪: ${PROMETHEUS_URL}"
    mapfile -d '' -t START_COMMAND < "/proc/${PROM_PID}/cmdline"
    ((${#START_COMMAND[@]} > 0)) || fail "无法读取 Prometheus 当前启动命令。"
fi

RUN_DIR="${PROJECT_ROOT}/backfill_output/run_${RUN_ID}"
mkdir -p "${RUN_DIR}"

echo "[1/6] 检测缺口并生成回灌包..."
cd "${PROJECT_ROOT}"
"${PYTHON_BIN}" -m ops.backfill_prometheus "${BACKFILL_ARGS[@]}" \
    --output-dir "${RUN_DIR}" --promtool "${PROMTOOL_BIN}" --prepare

BLOCKS_DIR="$(find "${RUN_DIR}" -maxdepth 1 -type d -name 'blocks_*' -print -quit)"
if [[ -z "${BLOCKS_DIR}" ]]; then
    echo "没有生成待导入 blocks，可能不存在缺口；Prometheus 未被停止。"
    exit 0
fi
mapfile -t BLOCK_DIRS < <(find "${BLOCKS_DIR}" -mindepth 1 -maxdepth 1 -type d -exec test -f '{}/meta.json' ';' -print)
((${#BLOCK_DIRS[@]} > 0)) || fail "回灌包中没有有效的 Prometheus block。"

BACKUP_DIR="${PROMETHEUS_DATA_DIR}_backup_${RUN_ID}"
DATA_SIZE_BYTES="$(du -sb "${PROMETHEUS_DATA_DIR}" | awk '{print $1}')"
AVAILABLE_BYTES="$(df -PB1 "$(dirname "${BACKUP_DIR}")" | awk 'NR==2 {print $4}')"
[[ -n "${DATA_SIZE_BYTES}" && -n "${AVAILABLE_BYTES}" && "${AVAILABLE_BYTES}" -gt "${DATA_SIZE_BYTES}" ]] || fail "磁盘空间不足，无法完整备份数据目录。"

echo "即将执行："
echo "  管理模式: ${MODE}"
echo "  数据目录: ${PROMETHEUS_DATA_DIR}"
echo "  完整备份: ${BACKUP_DIR}"
echo "  导入 block 数: ${#BLOCK_DIRS[@]}"
if [[ "${ASSUME_YES}" -ne 1 ]]; then
    read -r -p "确认短暂停止 Prometheus 并继续？输入 yes: " CONFIRMATION
    [[ "${CONFIRMATION}" == "yes" ]] || { echo "已取消，Prometheus 未被停止。"; exit 0; }
fi

PROMETHEUS_STOPPED=0
restart_on_exit() {
    if [[ "${PROMETHEUS_STOPPED}" -eq 1 ]]; then
        echo "检测到异常退出，正在尝试重新启动 Prometheus..." >&2
        start_prometheus || true
    fi
}
trap restart_on_exit EXIT

echo "[2/6] 停止 Prometheus..."
stop_prometheus "${PROM_PID}"
PROMETHEUS_STOPPED=1

echo "[3/6] 备份完整数据目录..."
cp -a "${PROMETHEUS_DATA_DIR}" "${BACKUP_DIR}"

echo "[4/6] 复制历史 blocks..."
for block_dir in "${BLOCK_DIRS[@]}"; do
    target="${PROMETHEUS_DATA_DIR}/$(basename "${block_dir}")"
    [[ ! -e "${target}" ]] || fail "目标 block 已存在: ${target}"
    cp -a "${block_dir}" "${PROMETHEUS_DATA_DIR}/"
done

echo "[5/6] 启动 Prometheus..."
start_prometheus || fail "Prometheus 启动失败；备份位于 ${BACKUP_DIR}。"
PROMETHEUS_STOPPED=0
trap - EXIT

echo "[6/6] 检查服务状态..."
if [[ "${MODE}" == "systemd" ]]; then
    systemctl is-active --quiet "${PROMETHEUS_SERVICE}" || fail "Prometheus 启动失败；备份位于 ${BACKUP_DIR}。"
else
    wait_ready || fail "Prometheus 未就绪；请检查 ${PROMETHEUS_DIR}/prometheus.log。"
fi

echo "回灌完成。"
echo "备份目录: ${BACKUP_DIR}"
echo "回灌输出: ${RUN_DIR}"
echo "请检查 Grafana 曲线，确认无误前不要删除备份。"
