# 账户监控服务（Account Monitor）

面向多交易所账户的统一监控服务。目前支持 Binance 与 Gate.io，可定时采集账户权益、记录分钟级净值快照、计算年化收益率，并通过 Prometheus、Grafana 和飞书完成指标展示与通知。服务同时提供账户申赎、分红及新增账户的 Web 操作页面。

## 主要功能

- 每分钟采集交易所账户权益并写入 CSV 快照
- 根据申购、赎回及分红记录维护账户单位净值
- 计算 24 小时、7 天、30 天等周期的年化收益率
- 暴露 Prometheus 指标，供 Grafana 仪表盘采集
- 定时发送飞书收益报告
- 通过 Web 页面管理申赎、分红和新增账户
- 支持从旧版 Binance/Gate 监控目录迁移历史快照
- 新增账户时自动追加对应的 Grafana Panel

## 项目结构

```text
account_monitor/
├── account_monitor_app.py          # FastAPI 服务入口
├── account_monitor.py              # 指标计算、日报及调度逻辑
├── accounts_config.example.yaml    # 账户配置示例
├── ops/                            # 运维工具
│   ├── bootstrap_minute_snapshots.py  # 历史分钟数据迁移
│   ├── sync_grafana_dashboards.py     # Grafana 面板同步
│   ├── backfill_prometheus.py          # Prometheus 历史数据回灌
│   └── backfill_prometheus.sh          # 自动停服、备份、导入和启动
├── start_account_monitor.sh        # Linux 后台启动脚本
├── core/                           # 配置、账户、交易所、指标和通知模块
├── nav_service/                    # Web 服务、路由及状态管理
├── templates/                      # 页面模板
├── static/                         # 静态资源
└── deploy/                         # Nginx/HTTPS 部署脚本与配置
```

## 环境要求

- Python 3.10 或更高版本
- Linux（推荐用于生产部署；启动及部署脚本均为 Bash）
- 对应交易所的只读 API Key
- 可选：Prometheus、Grafana、飞书机器人

项目目前未提供依赖锁定文件。请根据源码安装运行依赖，主要包括 FastAPI、Uvicorn、Pandas、PyYAML、Prometheus Client、Jinja2、python-multipart、requests、ccxt，以及 Gate.io SDK。

## 快速开始

### 1. 创建虚拟环境并安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install fastapi uvicorn pandas pyyaml prometheus-client jinja2 python-multipart requests ccxt gate-api
```

### 2. 创建账户配置

```bash
cp accounts_config.example.yaml accounts_config.yaml
```

编辑 `accounts_config.yaml`：

```yaml
Blacklist: []

Example_Account:
  key: replace_with_api_key
  secret: replace_with_secret_key
  initial_unit: 1000000
  principal: 1000000
  account_type: account
  exchange: Binance
  interest_rate: 0
  client: ExampleClient
  ccy: USDT
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `key` / `secret` | 交易所 API 凭证 |
| `initial_unit` | 账户初始份额 |
| `principal` | 当前本金；未配置时兼容使用 `initial_unit`，赎回后自动扣减并写回配置 |
| `account_type` | 账户类型 |
| `exchange` | 交易所，当前支持 `Binance`、`Gate` |
| `interest_rate` | 利率或计息参数 |
| `client` | 客户名称 |
| `ccy` | 计价币种，如 `USDT` 或 `BTC` |
| `Blacklist` | Gate 账户估值时忽略缺失价格的币种列表 |

Gate 估值遇到缺少 USDT 价格的币种时，会将该币种从当次权益快照中跳过并记录 `WARNING` 日志，但不会发送飞书告警。黑名单中的币种会静默跳过。

> `accounts_config.yaml` 包含敏感凭证，已设计为仅保留在服务器本地。请勿提交到 Git，也不要给 API Key 开启不必要的交易或提现权限。

### 3. 创建本地敏感配置

所有通用 API 凭证统一放在 `config/local_secrets.py`。该文件已写入 `.gitignore`，不会提交到 Git。首次部署时执行：

```bash
cp config/local_secrets.example.py config/local_secrets.py
```

然后编辑 `config/local_secrets.py`：

```python
FEISHU_BOT_WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/..."
FEISHU_BOT_WEBHOOK_URL_1 = ""
FEISHU_APP_ID = ""
FEISHU_APP_SECRET = ""

GRAFANA_API_TOKEN = ""
GRAFANA_USER = ""
GRAFANA_PASSWORD = ""
```

敏感文件的职责如下：

| 文件 | 是否提交 Git | 内容 |
| --- | --- | --- |
| `config/settings.py` | 是 | 路径、端口、调度时间等非敏感配置，以及本地密钥读取逻辑 |
| `config/local_secrets.example.py` | 是 | 不含真实值的配置模板 |
| `config/local_secrets.py` | 否 | 飞书和 Grafana 等真实凭证 |
| `accounts_config.yaml` | 否 | Binance、Gate 等各账户的 API Key 与 Secret |

账户密钥继续放在 `accounts_config.yaml`，因为它还包含账户份额、币种和客户等结构化信息；不与通用服务凭证混放。

#### 飞书是否需要 App API

- 仅发送文字或卡片到群机器人：只需 `FEISHU_BOT_WEBHOOK_URL`，不需要 `APP_ID` 和 `APP_SECRET`。
- 向飞书上传图片并把图片用于消息卡片：需要 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，服务会用它们获取 tenant access token。
- 需要同时通知两个机器人时，再填写 `FEISHU_BOT_WEBHOOK_URL_1`。

### 4. 配置非敏感环境变量

运行参数仍通过环境变量设置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MONITOR_NAV_HOST` | `127.0.0.1` | 服务监听地址 |
| `MONITOR_NAV_PORT` | `7007` | 服务监听端口 |
| `MONITOR_SERVICE_NAME` | `account_monitor` | PID 文件使用的服务名 |
| `PYTHON_BIN` | `$HOME/.venv/bin/python` | 启动脚本使用的 Python |
| `RUNTIME_LOG_LEVEL` | `WARNING` | 运行日志级别 |
| `BINANCE_DAILY_HOUR` | `10` | 每日任务执行小时 |
| `BINANCE_DAILY_MINUTE` | `31` | 每日任务执行分钟 |
| `BINANCE_RUN_DAILY_ON_STARTUP` | `1` | 启动时是否执行日报任务 |
| `GRAFANA_URL` | `http://127.0.0.1:3000` | Grafana 地址 |
| `GRAFANA_PROMETHEUS_UID_ACCOUNT_MONITOR` | 空 | Prometheus 数据源 UID |
| `GRAFANA_PROMETHEUS_INSTANCE_ACCOUNT_MONITOR` | `localhost:7007` | Prometheus 实例标签 |

敏感值不再通过 `config/settings.py` 的默认值或普通启动命令传入。

### 5. 启动服务

直接运行：

```bash
python account_monitor_app.py
```

或使用后台启动脚本：

```bash
chmod +x start_account_monitor.sh
./start_account_monitor.sh
```

启动脚本会：

1. 检查 PID，避免重复启动；
2. 在快照目录为空时执行历史数据迁移；
3. 将服务放到后台运行；
4. 把 PID 写入 `runtime_logs/`；启动时生成 `runtime_YYYYMMDD_HHMMSS.log`，跨天后切换到新日期的 `runtime_YYYYMMDD_000000.log`，并在每行标注日志来源。Uvicorn access log 默认关闭。

启动后可访问：

- 操作页面：`http://127.0.0.1:7007/operations`
- 账户表格：`http://127.0.0.1:7007/accounts`
- 账户 JSON API：`http://127.0.0.1:7007/api/accounts`
- 新增账户：`http://127.0.0.1:7007/accounts/new`
- Prometheus 指标：`http://127.0.0.1:7007/metrics`

## 数据与指标

分钟快照保存在：

```text
minute_snapshots/<exchange>_<account>_<ccy>_minute_snapshot.csv
```

快照固定使用 8 列：`timestamp`、`actual_equity`、`total_unit`、`net_value`、`dividend_amount`、`interest_deduction`、`withdraw_amount`、`subscription_amount`。如果旧文件仍为 6 列，或已经混入新版 8 列数据行，应先停止监控服务并执行 `python -m ops.migrate_snapshot_csv_to_8_columns`；详细迁移规则见 [运维工具说明](ops/README.md#分钟快照升级为-8-列)。

账户币种由 `ccy` 决定：`BTC` 账户返回 BTC 计价权益，其他账户默认保留 USDT 计价权益。报告分组会根据交易所、账户名前缀和币种动态生成，例如 `Binance_Loro_USDT`。

### Prometheus 指标及计算口径

每个账户会创建一组 Gauge，指标名前缀为 `<exchange_label>_<account_name>_`。下式中：

- E<sub>t</sub>：最新分钟快照的 `actual_equity`；
- N<sub>t</sub>：分钟快照的 `net_value`；
- M<sub>d</sub>：日期 d 在北京时间 17:00–18:00（CSV 中为 UTC 09:00–10:00）的 `net_value` 中位数；
- Q<sub>d</sub>：最近一个有效日期在同一时段的 `actual_equity` 中位数；
- `D`、`S`：快照文件中 `dividend_amount`、`subscription_amount` 的累计值；
- 所有收益率指标均直接以百分数写入，例如 `12.5` 表示 `12.5%`，并采用简单线性年化，不是复利年化。

| 指标后缀 | 更新频率 | Grafana | 计算内容 |
| --- | --- | --- | --- |
| `actual_equity` | 每分钟 | ✅ 正在使用 | 最新分钟快照的实际权益；同时用于年化收益 Panel 和净值 Panel。净值 Panel 会过滤不大于 `1e-7` 的值。 |
| `annualized_return_1m` | 每分钟 | ✅ 正在使用 | 名称保留为 `1m`，实际是最新净值相对约 24 小时前最后一条快照的点对点年化。Grafana 图例为 `24h AR(%)`。 |
| `annualized_return_1h` | 每分钟 | ✅ 正在使用 | 当前一小时净值中位数，相对前一天同一小时窗口中位数的年化收益。Grafana 图例为 `24h MED-AR(%)`。 |
| `annualized_return_24h` | 每日 | ✅ 正在使用 | 最近单日窗口内，日报日中位数的首尾简单年化收益。Grafana 图例为 `1-Day AR(%)`。 |
| `annualized_return_7d` | 每日 | ✅ 正在使用 | 最近 7 日窗口内，日报日中位数的首尾简单年化收益。Grafana 图例为 `7-Day AR(%)`。 |
| `annualized_return_30d` | 每日 | ✅ 正在使用 | 最近 30 日窗口内，日报日中位数的首尾简单年化收益。Grafana 图例为 `30-Day AR(%)`。 |
| `cumulative_return` | 每日 | — 当前脚本未使用 | 建仓以来，计入累计分红和累计申购后的简单年化累计收益。 |
| `post_dividend_annualized_return` | 每日 | — 当前脚本未使用 | 最近一次分红的下一天起，日报日中位数首尾之间的简单年化收益。无分红或不足两天时无有效值。 |
| `report_actual_equity` | 每日 | — 当前脚本未使用 | Q<sub>d</sub>，即最近有效日北京时间 17:00–18:00 的实际权益中位数；用于飞书日报展示及组合加权。 |

这里的“Grafana 正在使用”以 `ops/sync_grafana_dashboards.py` 当前生成的 PromQL Target 为准。已有 Dashboard 中人工添加或保留的 Panel 可能还会引用其他指标。

具体公式如下。公式中的收益率结果已经乘以 `100%`，即指标值 `12.5` 表示 `12.5%`。

**最新实际权益**

> E<sub>t</sub>

**24 小时点对点年化（指标名 `annualized_return_1m`）**

> (最新净值 ÷ 约 24 小时前的净值 − 1) × 365 × 100%

**一小时中位数口径年化**

> (当前一小时净值中位数 ÷ 前一天同一小时净值中位数 − 1) × 365 × 100%

**p 日收益年化（p = 1、7 或 30）**

> (窗口末日中位净值 ÷ 窗口首日中位净值 − 1) ÷ p × 365 × 100%

**建仓以来累计收益**

> (最新实际权益 + 累计分红 − 累计申购) ÷ `initial_unit` − 1

**建仓以来年化累计收益**

> 建仓以来累计收益 × 365 ÷ 建仓天数 × 100%

**分红后年化收益**

> (分红后末日中位净值 ÷ 分红后首日中位净值 − 1) ÷ 首尾实际间隔天数 × 365 × 100%

### 资金流入流出如何处理

申购、赎回、分红和扣息页面处理的是**监控账本**，不会向交易所发起真实划款。应先在线下或交易所完成资金操作，待分钟权益快照记录到资金变化后，再在 Web 页面登记对应日期。

系统使用“份额法”隔离外部资金流对收益率的影响。设事件前一条有效快照的单位净值为 N<sub>before</sub>，资金金额为 A，事件所在行原总份额为 U<sub>old</sub>：

**申购（资金流入）**

> 新增份额 = A ÷ N<sub>before</sub>
>
> U<sub>new</sub> = U<sub>old</sub> + 新增份额

**赎回、分红或扣息（资金流出）**

> 减少份额 = A ÷ N<sub>before</sub>
>
> U<sub>new</sub> = U<sub>old</sub> − 减少份额

**事件发生后的单位净值**

> 事件行新净值 = 登记操作时从交易所获取的最新实际权益 ÷ U<sub>new</sub>
>
> 事件后的历史行净值 = 该行记录的实际权益 ÷ U<sub>new</sub>

这样，单纯由入金或出金造成的 `actual_equity` 跳变会被对应的份额变化抵消，后续收益率仍主要反映账户自身的盈亏变化。

具体处理流程如下：

1. 用户提交账户、事件日期和金额，金额必须大于 0。
2. 系统读取该日期的分钟快照，以当天相邻快照中 `net_value` 绝对变化最大的一行作为推断的事件时点。
3. 使用事件时点之前最后一条净值作为 N<sub>before</sub>，计算增加或减少的份额。
4. 登记时重新向交易所查询一次最新实际权益；在事件行写入 `subscription_amount`、`withdraw_amount`、`dividend_amount` 或 `interest_deduction`，并用该最新权益更新事件行的 `total_unit` 和 `net_value`。
5. 将事件时点之后已有快照的 `total_unit` 统一更新为 U<sub>new</sub>，并按“实际权益 ÷ 新总份额”重算这些快照的净值；后续新快照也沿用最新总份额。

各类事件对配置及收益指标的影响：

| 事件 | 份额变化 | `principal` 变化 | CSV 记录列 | 累计收益公式中的处理 |
| --- | --- | --- | --- | --- |
| 申购 | 增加 | 增加并写回 `accounts_config.yaml` | `subscription_amount` | 从最终权益中扣除累计申购，避免将入金当成收益 |
| 赎回 | 减少 | 减少并写回 `accounts_config.yaml` | `withdraw_amount` | 当前 `cumulative_return` 公式未单独加回累计赎回 |
| 分红 | 减少 | 不变 | `dividend_amount` | 加回累计分红；同时作为“分红后年化收益”的起算事件 |
| 扣息 | 减少 | 不变 | `interest_deduction` | 当前 `cumulative_return` 公式未单独加回累计扣息 |

赎回时会校验 `withdrawal_amount <= principal`，允许全部赎回后配置本金归零，但不允许本金变为负数；所有事件处理后仍要求总份额大于最小有效阈值。`initial_unit` 保持初始值不变，`principal` 才是随申购、赎回调整的当前本金。

> 注意：事件时间不是用户精确输入的时分秒，而是根据所选日期内最大净值跳变推断。因此应确认该日期存在事件前后的分钟快照，且当天没有更大的无关净值异常。若同一天有多次资金变动，建议处理后检查 CSV 中识别出的事件行、份额和净值是否符合预期。

当输入缺失、不是有限数、分母绝对值过小或窗口数据不足时，对应 Gauge 通常写入 `NaN`；部分没有可计算历史的日级指标会保持原值。`actual_equity` 与 `report_actual_equity` 的区别是：前者取最新分钟值，后者取日报时段中位数。

### 飞书日报计算口径

飞书按“交易所 + 账户组 + 计价币种”分组生成卡片。单账户表格和柱状图直接读取上述 `report_actual_equity`、`annualized_return_24h`、`annualized_return_7d` 和 `annualized_return_30d`，不再重复计算单账户收益。

设组内有效账户为 i，日报权益为 Q<sub>i</sub>，对应周期的账户年化收益率为 r<sub>i,p</sub>。飞书卡片采用以下公式：

**组合规模**

> Σ Q<sub>i</sub>

**p 周期组合年化收益（p = 24h、7d 或 30d）**

> Σ(Q<sub>i</sub> × r<sub>i,p</sub>) ÷ Σ Q<sub>i</sub>

**账户资金成本**

> interest_rate<sub>i</sub> × 100%

**组合资金成本**

> Σ(Q<sub>i</sub> × 账户资金成本<sub>i</sub>) ÷ Σ Q<sub>i</sub>

**单账户超额收益**

> 账户 24h 年化收益 − 组合资金成本

**组合超额收益**

> 组合 24h 年化收益 − 组合资金成本

若没有有效权益但存在利率配置，组合资金成本会退化为各账户资金成本的算术平均。

组合计算会跳过权益或收益率无效、以及权益不大于最小有效阈值的账户。卡片中的“当日收益”实际指 `annualized_return_24h` 的简单年化百分比，不是未年化的单日收益；账户按该值从高到低排列。

Prometheus 可按如下方式配置抓取：

```yaml
scrape_configs:
  - job_name: account-monitor
    static_configs:
      - targets: ["127.0.0.1:7007"]
```

## 运维工具

Grafana 同步、历史快照迁移和 Prometheus 回灌脚本统一放在 `ops/`。请从项目根目录使用 `python -m ops.<工具名>` 执行，完整参数和安全操作说明见 [`ops/README.md`](ops/README.md)。

## Nginx 与 HTTPS 部署

首次在 Linux 服务器部署时：

```bash
chmod +x deploy/one_click_https_deploy.sh start_account_monitor.sh
DOMAIN=risk.example.com \
CERTBOT_EMAIL=admin@example.com \
./deploy/one_click_https_deploy.sh
```

执行前请确保：

- 域名已经解析到目标服务器；
- 防火墙已开放 TCP 80 和 443；
- 7007 端口仅作为本机 Nginx 上游，不直接暴露到公网。

后续仅更新 Nginx 配置时：

```bash
DOMAIN=risk.example.com ./deploy/deploy_nginx_https.sh
```

可使用以下命令检查部署状态：

```bash
sudo nginx -t
systemctl status nginx
curl -I https://risk.example.com/operations
```

## 添加新交易所

1. 在 `core/exchange_accounts/` 中新增交易所实现，并继承 `BaseExchangeAccount`；
2. 实现 `get_actual_equity()`，按需重写 `from_account_info()`；
3. 在 `core/exchange_accounts/__init__.py` 导出新类；
4. 在 `core/account_registry.py` 的交易所映射中注册；
5. 如需迁移旧数据，在 `ops/bootstrap_minute_snapshots.py` 中增加历史目录；
6. 使用新交易所账户配置验证快照、指标和 Web 操作流程。

基础语法检查示例：

```bash
python -m py_compile \
  core/account_registry.py \
  core/exchange_accounts/base.py \
  core/exchange_accounts/<exchange>.py \
  nav_service/state.py
```

## 安全提示

- 交易所 Key、飞书密钥和 Grafana Token 均不得提交到仓库；
- 生产环境应使用只读、限制 IP 且无提现权限的交易所凭证；
- `/accounts/new`、`/operations` 等管理页面应由 Nginx、VPN 或统一认证保护；
- 上线前请确认 `accounts_config.yaml`、日志和分钟快照的文件权限；
- 修改账户配置或净值历史前请先备份数据。
