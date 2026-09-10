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
| `ops.migrate_snapshot_csv_to_8_columns` | 将旧版或混合格式的分钟 CSV 一次性迁移为 8 列 | 是，会原子替换 CSV |
| `ops.sync_grafana_dashboards` | 创建或修复 Grafana Panel | 是；`--dry-run` 除外 |
| `ops.backfill_prometheus` | 检测 CSV/Prometheus 缺口并生成回灌 blocks | 默认否 |
| `ops/backfill_prometheus.sh` | 一键停服、备份、导入并重启 Prometheus | 是，高风险操作 |
| `ops.backfill_bigquery_returns` | 检测并回填 BigQuery 日收益缺口 | 默认否 |
| `ops.correct_rwusd_total_profit` | 审计并修正误计入净值的 RWUSD 历史累计收益 | 默认否 |

## 修正 RWUSD 历史累计收益

旧版 Binance 净值逻辑把 RWUSD 接口的 `rwusdAmount + totalProfit` 一起计入
`actual_equity`。`totalProfit` 是账户历史累计字段，复用旧账户时会把前一使用者的
RWUSD 收益带进新策略。当前净值逻辑只保留 `rwusdAmount`；本工具用于审计账户并
修正已经写入分钟 CSV 的历史数据。

先审计全部 Binance 账户的当前字段，不读取或修改 CSV：

```bash
python -m ops.correct_rwusd_total_profit --all
```

指定账户和策略起始时间进行 dry-run：

```bash
python -m ops.correct_rwusd_total_profit \
  --accounts Brioni_41 \
  --start "2026-07-18 00:00:00" \
  --csv-timezone UTC
```

脚本会先确认修正窗口内没有新的 RWUSD 收益、申购或赎回，再通过 Binance
`1m` Kline 查询每行对应的 `USDCUSDT` 和 `BTCUSDT` 收盘价。BTC 账户每行扣除：

```text
totalProfit × USDCUSDT(分钟) / BTCUSDT(分钟)
```

USDT 账户每行扣除 `totalProfit × USDCUSDT(分钟)`。`total_unit` 保持不变，
`net_value` 使用修正后的 `actual_equity / total_unit` 重算。dry-run 会把逐行价格、
扣减金额和修正前后数值写入 `runtime_logs/rwusd_total_profit_*.csv`，但不修改源 CSV。

审阅报告后，先停止账户监控，增加 `--execute` 才会原子替换源 CSV：

```bash
python -m ops.correct_rwusd_total_profit \
  --accounts Brioni_41 \
  --start "2026-07-18 00:00:00" \
  --csv-timezone UTC \
  --execute
```

执行前会检查运行中的 PID；执行时在源 CSV 同目录创建 `.bak` 备份和 JSON 修正
标记，避免重复执行。若窗口内存在任何 RWUSD 活动，脚本会拒绝修正，因为当前
`totalProfit` 不能安全代表每一个历史分钟。

分钟 K 线可以还原当时的市场价格，但不能精确复现旧程序请求发生那一秒的 ticker。
脚本使用该分钟收盘价，通常是现有公开数据能稳定复现的最细口径。

## 回填 BigQuery 日收益

工具按 `(date, account)` 查询 `BN_Return_temp` 的已有记录，从分钟快照重算历史收益，只选择缺失键。默认使用旧 Binance monitor 的分钟 CSV，以保持历史表原有的账户估值口径；切换后的日期才建议使用 `--source account-monitor`。

先执行 dry-run。它会计算但不会写入 BigQuery，并在 `runtime_logs/` 生成逐日期、逐账户的审计 CSV：

```bash
python -m ops.backfill_bigquery_returns \
  --start-date 2026-07-10 \
  --end-date 2026-07-23
```

重点检查报告中的状态：

- `ready`：源数据可以计算，且目标键缺失。
- `already_exists`：目标键已有一行，默认跳过。
- `uncomputable`：当天或向前 30 天的有效参考点不足。
- `source_error`：分钟 CSV 缺失或无法读取。

确认 `ready` 数量和账户口径后，增加 `--execute` 才会通过 MERGE 写入：

```bash
python -m ops.backfill_bigquery_returns \
  --start-date 2026-07-10 \
  --end-date 2026-07-23 \
  --execute
```

常用参数：

- `--accounts Brioni_27 Zara_101`：只处理指定账户，也支持逗号分隔。
- `--source account-monitor`：改用新服务的 `minute_snapshots/`。
- `--report /path/report.csv`：指定审计报告位置。
- `--replace-existing`：也重算并 MERGE 已存在的键，只有确认要覆盖时使用。

安全限制：

- 结束日期必须早于 UTC 当天，避免把未结束的当天数据写成日结果。
- 单次最多处理 366 天。
- 执行前发现目标表存在重复 `(date, account)` 键时会拒绝写入。
- 不加 `--execute` 永远不会修改 BigQuery。

## 历史分钟快照迁移

启动脚本会在 `minute_snapshots/` 缺失或为空时自动执行：

```bash
python -m ops.bootstrap_minute_snapshots
```

工具会从 `config/settings.py` 的 `LEGACY_SNAPSHOT_DIRS` 查找旧 CSV。目标文件已有不少于来源文件的行数时会跳过，不会覆盖更新的数据。

如需支持新的旧目录，应先修改 `LEGACY_SNAPSHOT_DIRS`，并在复制前备份现有 `minute_snapshots/`。

## 分钟快照升级为 8 列

当前业务代码只使用以下固定 8 列：

```text
timestamp,actual_equity,total_unit,net_value,dividend_amount,interest_deduction,withdraw_amount,subscription_amount
```

旧快照可能只有 6 列，或者已经出现“6 列表头、6/8 列数据行混合”的情况。此时 `pandas.read_csv()` 会报 `Expected 6 fields ... saw 8`，导致申购、赎回、分红或扣息无法读取整份快照。

先停止账户监控服务，避免迁移期间继续追加记录，然后在项目根目录执行：

```bash
python -m ops.migrate_snapshot_csv_to_8_columns
```

默认迁移 `minute_snapshots/` 下的全部 CSV。也可以只迁移指定文件：

```bash
python -m ops.migrate_snapshot_csv_to_8_columns minute_snapshots/example.csv
```

迁移规则：

- 旧 6 列行保留原有数据，在 `dividend_amount` 后补空的 `interest_deduction` 和 `withdraw_amount`，原 `subscription_amount` 移到第 8 列。
- 已经是 8 列的行保持不变。
- 遇到其他列数或未知表头时立即失败，不覆盖原文件。
- 每个文件先写入同目录临时文件，全部成功后再原子替换原文件。

迁移成功并确认所有文件均为 8 列后，再启动账户监控服务。业务读取逻辑不会继续兼容 6 列格式。

## Grafana Panel 管理

新增账户成功后，服务只为该账户追加年化收益和净值 Panel。完整初始化或人工修复使用：

```bash
export GRAFANA_URL=http://127.0.0.1:3000
python -m ops.sync_grafana_dashboards --dry-run
python -m ops.sync_grafana_dashboards
```

默认模式追加缺失账户 Panel，保留已有配置。账户名称直接读取 `accounts_config.yaml` 的账户键（如 `Piana_106`）；飞书原样显示，Grafana 去掉下划线且不显示客户名（如 `BN_Piana106_USDT`）。写入前在 `runtime_data/grafana_backups/` 保存备份。新生成的净值 Panel 会过滤 `actual_equity <= 1e-7`；年化指标查询会展示 `NaN`，并将 `±Inf` 转换为 `NaN`。

仅在明确需要按账户配置重建整个 Dashboard 时使用：

```bash
python -m ops.sync_grafana_dashboards --force-rebuild
```

`--force-rebuild` 会覆盖目标 Dashboard 的全部 Panel，包括人工 Panel。执行前必须先备份 Dashboard。

认证信息从 `config/local_secrets.py` 或相关环境变量读取，支持 Grafana API Token，也支持用户名和密码。

## 从分钟 CSV 回灌 Prometheus

### Linux 一键回灌（推荐）

如果 Prometheus 是从官方压缩包解压后直接运行的，只需指定它的目录。脚本会自动使用目录里的 `promtool`、`prometheus`、`prometheus.yml` 和 `data/`，并按当前进程原有参数重启。

```bash
sudo bash ops/backfill_prometheus.sh \
  --prometheus-dir /home/ec2-user/prometheus-3.3.0-rc.0.linux-arm64 \
  --python-bin /home/ec2-user/.venv/bin/python \
  --account Piana_104 \
  --start 2025-07-14T00:00:00Z \
  --end 2026-07-14T20:00:00Z
```

复制多行命令时，反斜杠 `\` 必须是每行最后一个字符，后面不能有空格，而且续行之间不能插入空行。也可以直接使用不易出错的单行形式：

```bash
sudo bash ops/backfill_prometheus.sh --prometheus-dir /home/ec2-user/prometheus-3.3.0-rc.0.linux-arm64 --python-bin /home/ec2-user/.venv/bin/python --account Piana_104 --start 2025-07-14T00:00:00Z --end 2026-07-14T20:00:00Z
```

脚本在停服务前会显示数据目录、备份目录和 block 数，并要求输入 `yes`。自动化环境可增加 `--yes`。

`--prometheus-dir` 模式要求 Prometheus 使用 `--web.enable-lifecycle` 启动，以便脚本通过 `/-/quit` 安全停止。你参考工程的启动命令已经包含该参数。

如果 Prometheus 由 systemd 管理，则改用数据目录和服务名：

```bash
sudo bash ops/backfill_prometheus.sh \
  --prometheus-data-dir /var/lib/prometheus \
  --prometheus-service prometheus \
  --python-bin /home/ec2-user/.venv/bin/python \
  --start 2025-07-14T00:00:00Z \
  --end 2026-07-14T20:00:00Z
```

脚本会在数据目录旁创建完整备份，例如 `/var/lib/prometheus_backup_20260715_120000`。发生异常时会尝试重新启动 Prometheus。确认 Grafana 数据正常前不要删除备份。

`promtool` 是 Prometheus 官方压缩包自带的工具。使用 `--prometheus-dir` 时无需安装，脚本会自动找到 `${prometheus目录}/promtool`；只有 systemd 安装且系统 PATH 中没有它时，才需要用 `--promtool /实际路径/promtool` 指定。

### 什么时候使用

CSV 明明有某段时间的账户权益，但 Grafana 曲线断开时使用。工具目前只补 `actual_equity` 曲线，不会修改 CSV。

### 最简单的操作

先检查会补多少数据，这一步不会写文件，也不会修改 Prometheus：

```bash
python -m ops.backfill_prometheus \
  --account Piana_104 \
  --start 2026-07-14T00:00:00Z \
  --end 2026-07-14T20:00:00Z
```

重点查看输出中的 `missing`：

```text
Piana_104: csv=1200, prometheus_minutes=900, missing=300
```

确认缺失数量合理后，用同一条命令加上 `--prepare`：

```bash
python -m ops.backfill_prometheus \
  --account Piana_104 \
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
