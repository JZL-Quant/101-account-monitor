#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ID="$(date +'%Y%m%d_%H%M%S')"

PROMETHEUS_DATA_DIR=""
PROMETHEUS_SERVICE="prometheus"
PYTHON_BIN="${PYTHON_BIN:-${PROJECT_ROOT}/.venv/bin/python}"
PROMTOOL_BIN="${PROMTOOL_BIN:-promtool}"
ASSUME_YES=0
BACKFILL_ARGS=()

usage() {
    cat <<'EOF'
用法：
  sudo bash ops/backfill_prometheus.sh \
    --prometheus-data-dir /var/lib/prometheus \
    --account BV10_LTP_USDT \
    --start 2026-07-14T00:00:00Z \
    --end 2026-07-14T20:00:00Z

脚本会自动生成回灌包、停止 Prometheus、备份数据目录、导入并重新启动服务。

脚本参数：
  --prometheus-data-dir PATH   Prometheus 的 storage.tsdb.path，必填
  --prometheus-service NAME    systemd 服务名，默认 prometheus
  --python-bin PATH            项目虚拟环境 Python
  --promtool PATH              promtool 路径，默认从 PATH 查找
  --yes                        跳过导入前确认
  -h, --help                   显示帮助

其他参数会原样传给 ops.backfill_prometheus，例如 --account、--start、--end、
--prometheus-url、--csv-timezone、--instance、--job 和 --label。
EOF
}

while (($#)); do
    case "$1" in
        --prometheus-data-dir)
            PROMETHEUS_DATA_DIR="${2:?--prometheus-data-dir 缺少路径}"
            shift 2
            ;;
        --prometheus-service)
            PROMETHEUS_SERVICE="${2:?--prometheus-service 缺少名称}"
            shift 2
            ;;
        --python-bin)
            PYTHON_BIN="${2:?--python-bin 缺少路径}"
            shift 2
            ;;
        --promtool)
            PROMTOOL_BIN="${2:?--promtool 缺少路径}"
            shift 2
            ;;
        --yes)
            ASSUME_YES=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            BACKFILL_ARGS+=("$1")
            shift
            ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: 需要 root 权限停止服务和写入 Prometheus 数据目录，请使用 sudo。" >&2
    exit 1
fi
if [[ -z "${PROMETHEUS_DATA_DIR}" ]]; then
    echo "ERROR: 必须指定 --prometheus-data-dir。" >&2
    usage >&2
    exit 1
fi
if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "ERROR: Python 不可执行: ${PYTHON_BIN}，请使用 --python-bin 指定虚拟环境。" >&2
    exit 1
fi
if ! command -v "${PROMTOOL_BIN}" >/dev/null 2>&1 && [[ ! -x "${PROMTOOL_BIN}" ]]; then
    echo "ERROR: 找不到 promtool: ${PROMTOOL_BIN}" >&2
    exit 1
fi
if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: 当前系统没有 systemctl。" >&2
    exit 1
fi

PROMETHEUS_DATA_DIR="$(readlink -f "${PROMETHEUS_DATA_DIR}")"
if [[ ! -d "${PROMETHEUS_DATA_DIR}" || "${PROMETHEUS_DATA_DIR}" == "/" ]]; then
    echo "ERROR: Prometheus 数据目录无效: ${PROMETHEUS_DATA_DIR}" >&2
    exit 1
fi
if ! systemctl is-active --quiet "${PROMETHEUS_SERVICE}"; then
    echo "ERROR: 服务 ${PROMETHEUS_SERVICE} 当前不是 active，已停止操作。" >&2
    exit 1
fi

RUN_DIR="${PROJECT_ROOT}/backfill_output/run_${RUN_ID}"
mkdir -p "${RUN_DIR}"

echo "[1/6] 检测缺口并生成回灌包..."
cd "${PROJECT_ROOT}"
"${PYTHON_BIN}" -m ops.backfill_prometheus \
    "${BACKFILL_ARGS[@]}" \
    --output-dir "${RUN_DIR}" \
    --promtool "${PROMTOOL_BIN}" \
    --prepare

BLOCKS_DIR="$(find "${RUN_DIR}" -maxdepth 1 -type d -name 'blocks_*' -print -quit)"
if [[ -z "${BLOCKS_DIR}" ]]; then
    echo "没有生成待导入 blocks；可能不存在缺口。Prometheus 未被停止。"
    exit 0
fi
mapfile -t BLOCK_DIRS < <(find "${BLOCKS_DIR}" -mindepth 1 -maxdepth 1 -type d -exec test -f '{}/meta.json' ';' -print)
if ((${#BLOCK_DIRS[@]} == 0)); then
    echo "ERROR: 回灌包中没有有效的 Prometheus block。" >&2
    exit 1
fi

BACKUP_DIR="${PROMETHEUS_DATA_DIR}_backup_${RUN_ID}"
DATA_SIZE_BYTES="$(du -sb "${PROMETHEUS_DATA_DIR}" | awk '{print $1}')"
BACKUP_PARENT="$(dirname "${BACKUP_DIR}")"
AVAILABLE_BYTES="$(df -PB1 "${BACKUP_PARENT}" | awk 'NR==2 {print $4}')"
if [[ -z "${DATA_SIZE_BYTES}" || -z "${AVAILABLE_BYTES}" || "${AVAILABLE_BYTES}" -le "${DATA_SIZE_BYTES}" ]]; then
    echo "ERROR: 备份所在磁盘空间不足。需要大于 ${DATA_SIZE_BYTES:-未知} 字节，可用 ${AVAILABLE_BYTES:-未知} 字节。" >&2
    exit 1
fi
echo "即将执行："
echo "  服务: ${PROMETHEUS_SERVICE}"
echo "  数据目录: ${PROMETHEUS_DATA_DIR}"
echo "  完整备份: ${BACKUP_DIR}"
echo "  当前数据量: ${DATA_SIZE_BYTES} 字节"
echo "  导入 block 数: ${#BLOCK_DIRS[@]}"
if [[ "${ASSUME_YES}" -ne 1 ]]; then
    read -r -p "确认停止 Prometheus 并继续导入？输入 yes: " CONFIRMATION
    if [[ "${CONFIRMATION}" != "yes" ]]; then
        echo "已取消，Prometheus 未被停止。"
        exit 0
    fi
fi

SERVICE_STOPPED=0
restart_on_exit() {
    if [[ "${SERVICE_STOPPED}" -eq 1 ]]; then
        echo "检测到异常退出，正在尝试重新启动 ${PROMETHEUS_SERVICE}..." >&2
        systemctl start "${PROMETHEUS_SERVICE}" || true
    fi
}
trap restart_on_exit EXIT

echo "[2/6] 停止 Prometheus..."
systemctl stop "${PROMETHEUS_SERVICE}"
SERVICE_STOPPED=1

echo "[3/6] 备份完整数据目录..."
cp -a "${PROMETHEUS_DATA_DIR}" "${BACKUP_DIR}"

echo "[4/6] 检查并复制历史 blocks..."
for block_dir in "${BLOCK_DIRS[@]}"; do
    target="${PROMETHEUS_DATA_DIR}/$(basename "${block_dir}")"
    if [[ -e "${target}" ]]; then
        echo "ERROR: 目标 block 已存在: ${target}" >&2
        exit 1
    fi
    cp -a "${block_dir}" "${PROMETHEUS_DATA_DIR}/"
done

echo "[5/6] 启动 Prometheus..."
systemctl start "${PROMETHEUS_SERVICE}"
SERVICE_STOPPED=0
trap - EXIT

echo "[6/6] 检查服务状态..."
if ! systemctl is-active --quiet "${PROMETHEUS_SERVICE}"; then
    echo "ERROR: Prometheus 启动失败。备份位于 ${BACKUP_DIR}" >&2
    systemctl status "${PROMETHEUS_SERVICE}" --no-pager >&2 || true
    exit 1
fi

echo "回灌完成。"
echo "备份目录: ${BACKUP_DIR}"
echo "回灌输出: ${RUN_DIR}"
echo "请检查 Prometheus 日志和 Grafana 曲线；确认无误前不要删除备份。"
