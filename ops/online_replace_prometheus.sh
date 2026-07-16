#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_ID="$(date -u +'%Y%m%d_%H%M%S')"

PROMETHEUS_DIR=""
PROMETHEUS_DATA_DIR=""
PROMETHEUS_URL="http://127.0.0.1:9090"
PYTHON_BIN="${PYTHON_BIN:-}"
PROMTOOL_BIN="${PROMTOOL_BIN:-}"
START="2026-01-01T00:00:00Z"
CSV_TIMEZONE="UTC"
JOB="account-monitor"
ASSUME_YES=0
ACCOUNT_ARGS=()
ACCOUNT_NAMES=()

usage() {
    cat <<'EOF'
用法：
  bash ops/online_replace_prometheus.sh \
    --prometheus-dir /home/ec2-user/prometheus-3.3.0-rc.0.linux-arm64 \
    --python-bin /home/ec2-user/account_monitor/.venv/bin/python

在线替换全部账户从 2026-01-01 到当前 UTC 时间前 4 小时的 actual_equity。
Prometheus 和账户监控不需要停止，但 Prometheus 必须启用 --web.enable-admin-api。

参数：
  --prometheus-dir PATH       Prometheus 解压/运行目录
  --prometheus-data-dir PATH  覆盖数据目录，默认 PROMETHEUS_DIR/data
  --prometheus-url URL        默认 http://127.0.0.1:9090
  --python-bin PATH           项目虚拟环境 Python
  --promtool PATH             默认 PROMETHEUS_DIR/promtool
  --start RFC3339             默认 2026-01-01T00:00:00Z
  --csv-timezone TZ           默认 UTC
  --job NAME                  默认 account-monitor
  --account NAME              只处理指定账户，可重复；默认处理全部账户
  --yes                       跳过删除前确认
  -h, --help                  显示帮助
EOF
}

fail() { echo "ERROR: $*" >&2; exit 1; }

while (($#)); do
    case "$1" in
        --prometheus-dir) PROMETHEUS_DIR="${2:?缺少路径}"; shift 2 ;;
        --prometheus-data-dir) PROMETHEUS_DATA_DIR="${2:?缺少路径}"; shift 2 ;;
        --prometheus-url) PROMETHEUS_URL="${2:?缺少 URL}"; shift 2 ;;
        --python-bin) PYTHON_BIN="${2:?缺少路径}"; shift 2 ;;
        --promtool) PROMTOOL_BIN="${2:?缺少路径}"; shift 2 ;;
        --start) START="${2:?缺少时间}"; shift 2 ;;
        --csv-timezone) CSV_TIMEZONE="${2:?缺少时区}"; shift 2 ;;
        --job) JOB="${2:?缺少名称}"; shift 2 ;;
        --account)
            ACCOUNT_NAMES+=("${2:?缺少账户名}")
            ACCOUNT_ARGS+=("--account" "$2")
            shift 2
            ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "未知参数: $1" ;;
    esac
done

command -v curl >/dev/null 2>&1 || fail "找不到 curl"
command -v date >/dev/null 2>&1 || fail "找不到 date"

for account_name in "${ACCOUNT_NAMES[@]}"; do
    [[ "${account_name}" =~ ^[A-Za-z0-9_]+$ ]] || fail "账户名只能包含字母、数字和下划线: ${account_name}"
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

END="$(date -u -d '4 hours ago' +'%Y-%m-%dT%H:%M:%SZ')"
[[ "$(date -u -d "${START}" +%s)" -lt "$(date -u -d "${END}" +%s)" ]] || fail "开始时间不能晚于截止时间"

curl -fsS "${PROMETHEUS_URL}/-/ready" >/dev/null || fail "Prometheus 未就绪: ${PROMETHEUS_URL}"

RUN_DIR="${PROJECT_ROOT}/backfill_output/online_replace_${RUN_ID}"
mkdir -p "${RUN_DIR}"

echo "[1/5] 生成全部账户的历史 blocks（此步骤可能耗时较长）..."
echo "      范围: ${START} -> ${END}"
cd "${PROJECT_ROOT}"
"${PYTHON_BIN}" -m ops.backfill_prometheus \
    "${ACCOUNT_ARGS[@]}" \
    --start "${START}" \
    --end "${END}" \
    --csv-timezone "${CSV_TIMEZONE}" \
    --job "${JOB}" \
    --output-dir "${RUN_DIR}" \
    --promtool "${PROMTOOL_BIN}" \
    --prepare

BLOCKS_DIR="$(find "${RUN_DIR}" -maxdepth 1 -type d -name 'blocks_*' -print -quit)"
[[ -n "${BLOCKS_DIR}" ]] || fail "没有生成 blocks"
mapfile -t BLOCK_DIRS < <(find "${BLOCKS_DIR}" -mindepth 1 -maxdepth 1 -type d -exec test -f '{}/meta.json' ';' -print)
((${#BLOCK_DIRS[@]} > 0)) || fail "没有生成有效 block"

if ((${#ACCOUNT_NAMES[@]} == 0)); then
    METRIC_REGEX=".*_actual_equity"
    ACCOUNT_DESCRIPTION="全部账户"
else
    ACCOUNT_PATTERN="$(IFS='|'; echo "${ACCOUNT_NAMES[*]}")"
    METRIC_REGEX=".*_(${ACCOUNT_PATTERN})_actual_equity"
    ACCOUNT_DESCRIPTION="${ACCOUNT_NAMES[*]}"
fi
SELECTOR="{__name__=~\"${METRIC_REGEX}\",job=\"${JOB}\"}"

echo "[2/5] 回灌包已完整生成，共 ${#BLOCK_DIRS[@]} 个 blocks。"
echo "即将在线删除并替换："
echo "  账户: ${ACCOUNT_DESCRIPTION}"
echo "  selector: ${SELECTOR}"
echo "  时间范围: ${START} -> ${END}"
echo "  数据目录: ${PROMETHEUS_DATA_DIR}"
if [[ "${ASSUME_YES}" -ne 1 ]]; then
    read -r -p "确认继续？输入 yes: " CONFIRMATION
    [[ "${CONFIRMATION}" == "yes" ]] || { echo "已取消；生成的回灌包保留在 ${RUN_DIR}"; exit 0; }
fi

echo "[3/5] 删除原有历史样本并清理 tombstones..."
HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    --data-urlencode "match[]=${SELECTOR}" \
    --data-urlencode "start=${START}" \
    --data-urlencode "end=${END}" \
    "${PROMETHEUS_URL}/api/v1/admin/tsdb/delete_series")"
[[ "${HTTP_CODE}" == "204" ]] || fail "delete_series 失败，HTTP ${HTTP_CODE}；请确认已启用 --web.enable-admin-api"

HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    "${PROMETHEUS_URL}/api/v1/admin/tsdb/clean_tombstones")"
[[ "${HTTP_CODE}" == "204" ]] || fail "clean_tombstones 失败，HTTP ${HTTP_CODE}"

echo "[4/5] 原子导入新 blocks，Prometheus 保持运行..."
STAGING_DIR="${PROMETHEUS_DATA_DIR}/.online-replace-${RUN_ID}"
mkdir "${STAGING_DIR}"
cleanup_staging() {
    if [[ -d "${STAGING_DIR}" ]]; then
        echo "未完成的临时目录保留在 ${STAGING_DIR}，请检查后清理。" >&2
    fi
}
trap cleanup_staging EXIT

for block_dir in "${BLOCK_DIRS[@]}"; do
    ulid="$(basename "${block_dir}")"
    [[ ! -e "${PROMETHEUS_DATA_DIR}/${ulid}" ]] || fail "目标 block 已存在: ${ulid}"
    cp -a "${block_dir}" "${STAGING_DIR}/${ulid}"
done
for staged_block in "${STAGING_DIR}"/*; do
    mv "${staged_block}" "${PROMETHEUS_DATA_DIR}/"
done
rmdir "${STAGING_DIR}"
trap - EXIT

echo "[5/5] 完成。Prometheus 未停止。"
echo "回灌范围: ${START} -> ${END}"
echo "回灌包保留在: ${RUN_DIR}"
echo "请检查 Prometheus 日志和 Grafana 历史曲线。"
