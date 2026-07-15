# 运维工具

本目录集中存放不参与常规请求处理的初始化、迁移、修复和回灌工具。所有命令都应在项目根目录、项目虚拟环境中执行：

```bash
cd /path/to/account_monitor
source .venv/bin/activate
```

## 工具列表

| 模块 | 用途 | 默认是否修改外部状态 |
| --- | --- | --- |
| `ops.bootstrap_minute_snapshots` | 从旧监控目录迁移分钟快照 | 是，会复制 CSV |
| `ops.sync_grafana_dashboards` | 创建或修复 Grafana Panel | 是；`--dry-run` 除外 |
| `ops.backfill_prometheus` | 检测 CSV/Prometheus 缺口并生成回灌 blocks | 默认否 |
| `ops/backfill_prometheus.sh` | 一键停服、备份、导入并重启 Prometheus | 是，高风险操作 |

## 历史分钟快照迁移

启动脚本会在 `minute_snapshots/` 缺失或为空时自动执行：

```bash
python -m ops.bootstrap_minute_snapshots
```

工具会从 `config/settings.py` 的 `LEGACY_SNAPSHOT_DIRS` 查找旧 CSV。目标文件已有不少于来源文件的行数时会跳过，不会覆盖更新的数据。

如需支持新的旧目录，应先修改 `LEGACY_SNAPSHOT_DIRS`，并在复制前备份现有 `minute_snapshots/`。

## Grafana Panel 管理

新增账户成功后，服务只为该账户追加年化收益和净值 Panel。完整初始化或人工修复使用：

```bash
export GRAFANA_URL=http://127.0.0.1:3000
python -m ops.sync_grafana_dashboards --dry-run
python -m ops.sync_grafana_dashboards
```

默认模式只追加标题不存在的账户 Panel，保留已有 Panel。新生成的净值 Panel 会过滤 `actual_equity <= 1e-7`，年化指标查询会排除 `NaN` 和 `±Inf`。

仅在明确需要按账户配置重建整个 Dashboard 时使用：

```bash
python -m ops.sync_grafana_dashboards --force-rebuild
```

`--force-rebuild` 会覆盖目标 Dashboard 的全部 Panel，包括人工 Panel。执行前必须先备份 Dashboard。

认证信息从 `config/local_secrets.py` 或相关环境变量读取，支持 Grafana API Token，也支持用户名和密码。

## 从分钟 CSV 回灌 Prometheus

### Linux 一键回灌（推荐）

确认 Prometheus 的数据目录后，可以用一条命令完成：缺口检测、生成回灌包、停止服务、完整备份、导入、启动和状态检查。

```bash
sudo bash ops/backfill_prometheus.sh \
  --prometheus-data-dir /var/lib/prometheus \
  --account BV10_LTP_USDT \
  --start 2026-07-14T00:00:00Z \
  --end 2026-07-14T20:00:00Z
```

脚本在停服务前会显示数据目录、备份目录和 block 数，并要求输入 `yes`。自动化环境可增加 `--yes`。

如果项目虚拟环境不在项目根目录的 `.venv`，需要明确指定：

```bash
--python-bin /home/ec2-user/.venv/bin/python
```

如果 systemd 服务名不是 `prometheus`，增加：

```bash
--prometheus-service prometheus-server
```

脚本会在数据目录旁创建完整备份，例如 `/var/lib/prometheus_backup_20260715_120000`。发生异常时会尝试重新启动 Prometheus。确认 Grafana 数据正常前不要删除备份。

### 什么时候使用

CSV 明明有某段时间的账户权益，但 Grafana 曲线断开时使用。工具目前只补 `actual_equity` 曲线，不会修改 CSV。

### 最简单的操作

先检查会补多少数据，这一步不会写文件，也不会修改 Prometheus：

```bash
python -m ops.backfill_prometheus \
  --account BV10_LTP_USDT \
  --start 2026-07-14T00:00:00Z \
  --end 2026-07-14T20:00:00Z
```

重点查看输出中的 `missing`：

```text
BV10_LTP_USDT: csv=1200, prometheus_minutes=900, missing=300
```

确认缺失数量合理后，用同一条命令加上 `--prepare`：

```bash
python -m ops.backfill_prometheus \
  --account BV10_LTP_USDT \
  --start 2026-07-14T00:00:00Z \
  --end 2026-07-14T20:00:00Z \
  --prepare
```

这一条命令会自动完成缺口检测、格式转换和回灌包生成。需要服务器已安装与 Prometheus 配套的 `promtool`。生成结果在：

```text
backfill_output/
├── blocks_<时间>/       # 要导入 Prometheus 的数据目录
└── IMPORT_<时间>.txt    # 本次回灌的导入说明
```

### 为什么没有直接自动写入 Prometheus

Prometheus 不提供普通接口修改过去的本地数据。导入历史数据必须直接操作它的数据目录，因此需要短暂停止服务。不同服务器的 Prometheus 数据目录、服务名称和权限都可能不同，脚本不会冒险自动停止或修改生产服务。

按照生成的 `IMPORT_<时间>.txt` 操作即可，核心步骤只有：

1. 找到 Prometheus 启动参数中的 `--storage.tsdb.path`。
2. 备份整个数据目录。
3. 停止 Prometheus。
4. 将 `blocks_<时间>/` 里面的目录复制到 `storage.tsdb.path`。
5. 启动 Prometheus 并检查 Grafana 曲线。

不要回灌最近3小时的数据，也不要重复导入同一个回灌包。

### 常见参数

- 不指定 `--account`：检查所有账户。
- Prometheus 不在本机：增加 `--prometheus-url http://服务器:9090`。
- CSV 时间是北京时间：增加 `--csv-timezone Asia/Shanghai`。
- 指标标签不同：使用 `--instance`、`--job` 或 `--label NAME=VALUE`。

### 名词解释（一般使用时可以忽略）

- **OpenMetrics**：脚本内部生成的临时文本格式，用来把 CSV 数据交给 Prometheus 官方工具。使用者不需要手工编辑。
- **TSDB blocks**：Prometheus 数据库实际使用的历史数据文件夹。`--prepare` 生成的 `blocks_<时间>/` 就是待导入的数据包。
