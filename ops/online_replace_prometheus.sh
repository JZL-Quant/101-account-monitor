#!/usr/bin/env bash
set -Eeuo pipefail

# Safe CSV -> Prometheus backfill, generalized from
# minute_snapshots/backfill_gate_bv4_prometheus.sh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ID="$(date -u +'%Y%m%d_%H%M%S')"

PROMETHEUS_DIR=""
PROMETHEUS_DATA_DIR=""
PROMETHEUS_URL="http://127.0.0.1:9090"
PYTHON_BIN="${PYTHON_BIN:-}"
PROMTOOL_BIN="${PROMTOOL_BIN:-}"
START="2026-01-01T00:00:00Z"
END=""
CSV_TIMEZONE="UTC"
ASSUME_YES=0
ACCOUNT_ARGS=()
ACCOUNT_NAMES=()
START_COMMAND=()
PROMETHEUS_STOPPED=0
DATA_MUTATED=0
BACKUP_DIR=""

usage() {
    cat <<'EOF'
用法：
  bash ops/online_replace_prometheus.sh \
    --prometheus-dir /home/ec2-user/prometheus-3.3.0-rc.0.linux-arm64 \
    --python-bin /home/ec2-user/account_monitor/.venv/bin/python \
    --account BV_1 \
    --start 2026-05-01T00:00:00Z

安全回灌 CSV 中缺失的 actual_equity，默认截止到当前 UTC 时间前 4 小时。
流程：生成并校验 blocks -> 停止 Prometheus -> 完整备份 -> 导入 -> 原命令重启
-> 查询验证。导入或验证失败时自动恢复完整备份。

参数：
  --prometheus-dir PATH       Prometheus 解压/运行目录（必填）
  --prometheus-data-dir PATH  数据目录，默认 PROMETHEUS_DIR/data
  --prometheus-url URL        默认 http://127.0.0.1:9090
  --python-bin PATH           项目虚拟环境 Python
  --promtool PATH             默认 PROMETHEUS_DIR/promtool
  --account NAME              指定账户，可重复；默认处理全部账户
  --start RFC3339             默认 2026-01-01T00:00:00Z
  --end RFC3339               默认当前 UTC 时间前 4 小时
  --csv-timezone TZ           默认 UTC
  --yes                       跳过停服前确认
  -h, --help                  显示帮助
EOF
}

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
require_command() { command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"; }

while (($#)); do
    case "$1" in
        --prometheus-dir) PROMETHEUS_DIR="${2:?缺少路径}"; shift 2 ;;
        --prometheus-data-dir) PROMETHEUS_DATA_DIR="${2:?缺少路径}"; shift 2 ;;
        --prometheus-url) PROMETHEUS_URL="${2:?缺少 URL}"; shift 2 ;;
        --python-bin) PYTHON_BIN="${2:?缺少路径}"; shift 2 ;;
        --promtool) PROMTOOL_BIN="${2:?缺少路径}"; shift 2 ;;
        --account)
            ACCOUNT_NAMES+=("${2:?缺少账户名}")
            ACCOUNT_ARGS+=("--account" "$2")
            shift 2
            ;;
        --start) START="${2:?缺少时间}"; shift 2 ;;
        --end) END="${2:?缺少时间}"; shift 2 ;;
        --csv-timezone) CSV_TIMEZONE="${2:?缺少时区}"; shift 2 ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "未知参数: $1" ;;
    esac
done

for command_name in awk cp curl date df du find flock mapfile mv pgrep readlink sed setsid; do
    require_command "${command_name}"
done

[[ -n "${PROMETHEUS_DIR}" ]] || fail "必须指定 --prometheus-dir"
PROMETHEUS_DIR="$(readlink -f "${PROMETHEUS_DIR}")"
[[ -d "${PROMETHEUS_DIR}" && "${PROMETHEUS_DIR}" != "/" ]] || fail "Prometheus 目录无效"

[[ -n "${PROMETHEUS_DATA_DIR}" ]] || PROMETHEUS_DATA_DIR="${PROMETHEUS_DIR}/data"
PROMETHEUS_DATA_DIR="$(readlink -f "${PROMETHEUS_DATA_DIR}")"
[[ -d "${PROMETHEUS_DATA_DIR}" && "${PROMETHEUS_DATA_DIR}" != "/" ]] || fail "Prometheus 数据目录无效"
[[ -w "${PROMETHEUS_DATA_DIR}" ]] || fail "当前用户不能写入 ${PROMETHEUS_DATA_DIR}"

[[ -n "${PROMTOOL_BIN}" ]] || PROMTOOL_BIN="${PROMETHEUS_DIR}/promtool"
[[ -x "${PROMTOOL_BIN}" ]] || fail "promtool 不可执行: ${PROMTOOL_BIN}"

if [[ -z "${PYTHON_BIN}" ]]; then
    for candidate in "${VIRTUAL_ENV:-}/bin/python" "${PROJECT_ROOT}/.venv/bin/python"; do
        if [[ "${candidate}" != "/bin/python" && -x "${candidate}" ]]; then
            PYTHON_BIN="${candidate}"
            break
        fi
    done
fi
[[ -n "${PYTHON_BIN}" && -x "${PYTHON_BIN}" ]] || fail "找不到虚拟环境 Python，请指定 --python-bin"

[[ -n "${END}" ]] || END="$(date -u -d '4 hours ago' +'%Y-%m-%dT%H:%M:%SZ')"
[[ "$(date -u -d "${START}" +%s)" -lt "$(date -u -d "${END}" +%s)" ]] || fail "开始时间不能晚于截止时间"

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
    for ((i = 0; i < 100; i++)); do
        curl -fsS "${PROMETHEUS_URL}/-/ready" >/dev/null 2>&1 && return 0
        sleep 0.2
    done
    return 1
}

stop_prometheus() {
    local pid="$1" i
    log "停止 Prometheus，PID=${pid}"
    curl -fsS -X POST "${PROMETHEUS_URL}/-/quit" >/dev/null || return 1
    for ((i = 0; i < 100; i++)); do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.2
    done
    return 1
}

start_prometheus() {
    log "使用原启动参数启动 Prometheus"
    (
        cd "${PROMETHEUS_DIR}"
        setsid -f "${START_COMMAND[@]}" >> prometheus.log 2>&1
    )
    wait_ready
}

restart_on_early_exit() {
    if [[ "${PROMETHEUS_STOPPED}" -eq 1 && "${DATA_MUTATED}" -eq 0 ]]; then
        log "检测到异常退出，正在重新启动未修改数据的 Prometheus" >&2
        start_prometheus || true
    fi
}

rollback() {
    local failed_dir="${PROMETHEUS_DIR}/data.failed_${RUN_ID}"
    local current_pid

    log "导入验证失败，开始恢复完整备份"
    current_pid="$(find_prometheus_pid || true)"
    if [[ -n "${current_pid}" ]]; then
        stop_prometheus "${current_pid}" || fail "无法停止 Prometheus，未执行自动回滚"
    fi
    [[ -d "${BACKUP_DIR}" ]] || fail "备份不存在，无法自动回滚: ${BACKUP_DIR}"
    mv "${PROMETHEUS_DATA_DIR}" "${failed_dir}"
    mv "${BACKUP_DIR}" "${PROMETHEUS_DATA_DIR}"
    DATA_MUTATED=0
    PROMETHEUS_STOPPED=1
    start_prometheus || fail "备份已恢复，但 Prometheus 启动失败；请检查 prometheus.log"
    PROMETHEUS_STOPPED=0
    fail "已回滚；失败数据保留在 ${failed_dir}"
}

PROM_PID="$(find_prometheus_pid || true)"
[[ -n "${PROM_PID}" ]] || fail "没有找到 ${PROMETHEUS_DIR}/prometheus 进程"
curl -fsS "${PROMETHEUS_URL}/-/ready" >/dev/null || fail "Prometheus 当前未就绪"
mapfile -d '' -t START_COMMAND < "/proc/${PROM_PID}/cmdline"
((${#START_COMMAND[@]} > 0)) || fail "无法读取 Prometheus 原启动命令"

LOCK_FILE="${PROMETHEUS_DIR}/.account_monitor_backfill.lock"
exec 9>"${LOCK_FILE}"
flock -n 9 || fail "已有回灌任务正在执行"

RUN_DIR="${PROJECT_ROOT}/backfill_output/safe_backfill_${RUN_ID}"
mkdir -p "${RUN_DIR}"

log "生成缺失历史数据：${START} -> ${END}"
cd "${PROJECT_ROOT}"
"${PYTHON_BIN}" -m ops.backfill_prometheus \
    "${ACCOUNT_ARGS[@]}" \
    --start "${START}" \
    --end "${END}" \
    --csv-timezone "${CSV_TIMEZONE}" \
    --output-dir "${RUN_DIR}" \
    --promtool "${PROMTOOL_BIN}" \
    --prepare

OPENMETRICS_FILE="$(find "${RUN_DIR}" -maxdepth 1 -type f -name '*.openmetrics' -print -quit)"
BLOCKS_DIR="$(find "${RUN_DIR}" -maxdepth 1 -type d -name 'blocks_*' -print -quit)"
if [[ -z "${BLOCKS_DIR}" ]]; then
    log "没有缺失数据，无需导入"
    exit 0
fi
[[ -n "${OPENMETRICS_FILE}" && -f "${OPENMETRICS_FILE}" ]] || fail "找不到 OpenMetrics 文件"

SAMPLE_COUNT="$(awk '!/^#/ && NF >= 3 {count++} END {print count + 0}' "${OPENMETRICS_FILE}")"
BLOCK_SAMPLES="$("${PROMTOOL_BIN}" tsdb list "${BLOCKS_DIR}" | awk 'NR > 1 {samples += $5} END {print samples + 0}')"
[[ "${SAMPLE_COUNT}" -gt 0 ]] || fail "OpenMetrics 中没有样本"
[[ "${BLOCK_SAMPLES}" -eq "${SAMPLE_COUNT}" ]] || fail "样本校验失败: openmetrics=${SAMPLE_COUNT}, blocks=${BLOCK_SAMPLES}"

mapfile -t BLOCK_DIRS < <(find "${BLOCKS_DIR}" -mindepth 1 -maxdepth 1 -type d -exec test -f '{}/meta.json' ';' -print)
((${#BLOCK_DIRS[@]} > 0)) || fail "没有有效 TSDB block"

DATA_BYTES="$(du -sb "${PROMETHEUS_DATA_DIR}" | awk '{print $1}')"
AVAILABLE_BYTES="$(df -PB1 "${PROMETHEUS_DIR}" | awk 'NR == 2 {print $4}')"
(( AVAILABLE_BYTES > DATA_BYTES )) || fail "磁盘空间不足，无法完整备份 Prometheus data"

log "准备导入 ${#BLOCK_DIRS[@]} 个 blocks，共 ${SAMPLE_COUNT} 条样本"
log "将完整备份 ${PROMETHEUS_DATA_DIR}，Prometheus 会短暂停止"
if [[ "${ASSUME_YES}" -ne 1 ]]; then
    read -r -p "确认继续？输入 yes: " CONFIRMATION
    CONFIRMATION="${CONFIRMATION//[[:space:]]/}"
    CONFIRMATION="${CONFIRMATION,,}"
    [[ "${CONFIRMATION}" == "yes" || "${CONFIRMATION}" == "y" ]] || { log "已取消，回灌包保留在 ${RUN_DIR}"; exit 0; }
fi

trap restart_on_early_exit EXIT
stop_prometheus "${PROM_PID}" || fail "Prometheus 未能安全停止"
PROMETHEUS_STOPPED=1

BACKUP_DIR="${PROMETHEUS_DATA_DIR}.backup_${RUN_ID}_before_backfill"
log "完整备份 ${PROMETHEUS_DATA_DIR} -> ${BACKUP_DIR}"
cp -a "${PROMETHEUS_DATA_DIR}" "${BACKUP_DIR}" || fail "备份失败"

DATA_MUTATED=1
log "复制历史 blocks"
for block_dir in "${BLOCK_DIRS[@]}"; do
    target="${PROMETHEUS_DATA_DIR}/$(basename "${block_dir}")"
    [[ ! -e "${target}" ]] || rollback
    cp -a "${block_dir}" "${PROMETHEUS_DATA_DIR}/" || rollback
done

start_prometheus || rollback
PROMETHEUS_STOPPED=0

log "逐条 series 验证导入结果"
while read -r selector verify_time; do
    RESPONSE="$(curl -fsSG "${PROMETHEUS_URL}/api/v1/query" \
        --data-urlencode "query=${selector}" \
        --data-urlencode "time=${verify_time}")" || rollback
    [[ "${RESPONSE}" != *'"result":[]'* ]] || rollback
done < <(awk '!/^#/ && NF >= 3 && !seen[$1]++ {print $1, $3}' "${OPENMETRICS_FILE}")

DATA_MUTATED=0
trap - EXIT
log "回灌和验证完成"
log "完整备份保留在: ${BACKUP_DIR}"
log "回灌输出保留在: ${RUN_DIR}"
