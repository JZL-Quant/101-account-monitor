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
├── bootstrap_minute_snapshots.py   # 历史分钟数据迁移工具
├── sync_grafana_dashboards.py      # Grafana 面板同步工具
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
| `MONITOR_SERVICE_NAME` | `account_monitor` | 日志及 PID 文件使用的服务名 |
| `PYTHON_BIN` | `$HOME/.venv/bin/python` | 启动脚本使用的 Python |
| `RUNTIME_LOG_LEVEL` | `ERROR` | 运行日志级别 |
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
4. 把 PID 和启动日志写入 `nv_logs/`。

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

账户币种由 `ccy` 决定：`BTC` 账户返回 BTC 计价权益，其他账户默认保留 USDT 计价权益。报告分组会根据交易所、账户名前缀和币种动态生成，例如 `Binance_Loro_USDT`。

Prometheus 可按如下方式配置抓取：

```yaml
scrape_configs:
  - job_name: account-monitor
    static_configs:
      - targets: ["127.0.0.1:7007"]
```

## Grafana Panel 管理

新增账户成功后，服务会为该账户追加年化收益和净值 Panel。它只处理本次新增的账户，不会覆盖、修改或恢复其他 Panel；因此在 Grafana 页面中的人工调整和删除会被保留。

`sync_grafana_dashboards.py` 不再由每日任务调用，仅用于首次初始化或人工修复。先在 `config/local_secrets.py` 中配置 `GRAFANA_API_TOKEN`（或用户名、密码），然后执行：

```bash
export GRAFANA_URL=http://127.0.0.1:3000
python sync_grafana_dashboards.py
```

默认模式只追加当前 Dashboard 中标题不存在的账户 Panel，已有 Panel 原样保留。可先预览：

新生成的净值 Panel 会过滤 `actual_equity <= 1e-7` 的无效值，并仅展示当前 Grafana 时间范围内处于 1% 至 99% 分位数之间的数据，以减少零值和极端异常值对曲线的影响。

```bash
python sync_grafana_dashboards.py --dry-run
```

只有明确需要清空人工 Panel 并按账户配置重建整个 Dashboard 时，才使用：

```bash
python sync_grafana_dashboards.py --force-rebuild
```

> `--force-rebuild` 会覆盖目标 Dashboard 的全部 Panel，属于破坏性操作。日常修复不要使用该参数。

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
5. 如需迁移旧数据，在 `bootstrap_minute_snapshots.py` 中增加历史目录；
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
