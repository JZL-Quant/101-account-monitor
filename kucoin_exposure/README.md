# KuCoin 现货 / 合约对冲监控

这是一个与现有 `account_monitor` 独立运行的服务。它默认监听
`127.0.0.1:8000`，每分钟通过 KuCoin UTA 接口查询当前子账户的统一账户
资产和 Futures 仓位，按合约 `multiplier` 换算成币数量，再计算现货与
合约是否对冲。

它有自己的配置、登录页面、定时任务、SQLite 数据库和运行日志。启动或
停止它不需要重启现有 `account_monitor`。

> KuCoin 当前官方 UTA 文档仍把这组接口标为持续开发状态，接口字段可能
> 变化。程序因此默认 `trading.enabled: false`，并在每次平仓前强制检查
> multiplier 换算。用于真实资金前应先用小仓位完成页面查询和平仓测试。

## 目录

重要文件都在 `kucoin_exposure/` 内：

```text
kucoin_exposure/
├─ config.yaml                 本机真实配置，包含密码和 API 凭证
├─ config.example.yaml         带注释的配置模板，可以提交到 Git
├─ requirements.txt            Python 依赖
├─ runtime_data/
│  └─ kucoin_exposure.sqlite3  敞口快照和操作记录
├─ runtime_logs/
│  └─ kucoin_exposure.log      运行日志
└─ templates/                  登录页和敞口页面
```

`config.yaml`、`runtime_data/` 和 `runtime_logs/` 已加入 `.gitignore`。
不要把真实 API Key、Secret、Passphrase 或页面密码提交到版本库。

## 快速启动

在项目根目录安装依赖：

```bash
python -m pip install -r kucoin_exposure/requirements.txt
```

编辑 `kucoin_exposure/config.yaml` 后启动：

```bash
python -m kucoin_exposure
```

浏览器访问：

```text
http://127.0.0.1:8000/login
```

停止服务时，在运行它的终端按 `Ctrl+C`。所有配置均在启动时读取，修改
`config.yaml` 后需要重启这个 KuCoin 服务，但不需要重启
`account_monitor`。

## 完整配置说明

下面按 `config.yaml` 中的顺序解释每一个配置项。

### `server`

控制独立网页服务监听在哪里。

```yaml
server:
  host: 127.0.0.1
  port: 8000
```

#### `server.host`

- 默认值：`127.0.0.1`
- 是否必填：否
- 修改后是否需要重启：是

`127.0.0.1` 表示只允许运行服务的这台机器访问，适合本机调试，也最安全。

如果要让同一局域网中的其他电脑访问，可以改成：

```yaml
host: 0.0.0.0
```

此时还需要：

- 在浏览器中使用服务器的局域网 IP，而不是 `0.0.0.0`；
- 确认操作系统防火墙是否允许端口；
- 使用足够强的页面密码；
- 不要直接把端口暴露到公网。

#### `server.port`

- 默认值：`8000`
- 是否必填：否
- 修改后是否需要重启：是

KuCoin 独立服务使用的端口。它与现有 `account_monitor` 的 `7007` 分开，
所以两个服务可以同时运行。

如果 `8000` 已被其他程序占用，可以换成其他未占用端口，例如 `8001`，
随后使用新端口访问页面。

### `login`

这里登录的是我们开发的本地监控页面，不是 KuCoin 官网，也不是 KuCoin
账户密码。它主要用于保护真实平仓按钮。

```yaml
login:
  username: admin
  password: change_me
  session_secret: replace_this_with_a_fixed_random_string_please
  session_days: 30
```

#### `login.username`

- 默认值：`admin`
- 是否必填：是
- 修改后是否需要重启：是

打开 `/login` 页面时输入的用户名。

#### `login.password`

- 默认值：`change_me`
- 是否必填：是
- 修改后是否需要重启：是

打开页面时输入的密码。当前第一版按需求采用最简单的本地比较方式，因此
密码以明文保存在 `config.yaml` 中。

务必修改默认密码。该密码与 KuCoin 登录密码、API Passphrase 没有关系。

#### `login.session_secret`

- 默认值：配置文件中的占位字符串
- 是否必填：是
- 最小长度：24个字符
- 修改后是否需要重启：是

用于给登录 Cookie 签名，防止用户伪造登录状态。它可以是任意足够长的
随机字符串，例如：

```yaml
session_secret: q9H7mP2sK8vN4xR6cT1wY5zL3bD0fG
```

第一次设置后尽量不要修改。只要这个值不变，即使 KuCoin 服务重启，
浏览器原来的登录状态仍然可以继续使用。修改它会让所有旧登录 Cookie
立即失效，需要重新登录。

它不是 KuCoin API Secret。

#### `login.session_days`

- 默认值：`30`
- 是否必填：否
- 修改后是否需要重启：是

登录 Cookie 的有效天数。`30` 表示登录成功后，正常情况下30天内不需要
重复登录。

以下情况仍会要求重新登录：

- 点击“退出”；
- 浏览器清除了 Cookie；
- 修改了 `session_secret`；
- Cookie 到期；
- 换了浏览器或电脑。

### `kucoin`

当前 KuCoin 子账户的 API 凭证。它们只在后端发起签名请求时使用，不会
写入网页，也不会发送到浏览器。

```yaml
kucoin:
  api_key: REPLACE_ME
  api_secret: REPLACE_ME
  api_passphrase: REPLACE_ME
  site_type: global
```

#### `kucoin.api_key`

- 是否必填：是
- 修改后是否需要重启：是

在 KuCoin API 管理页面创建 API 时得到的 Key。

#### `kucoin.api_secret`

- 是否必填：是
- 修改后是否需要重启：是

创建 API 时得到的 Secret，用于生成请求签名。不要填写 KuCoin 登录密码。

#### `kucoin.api_passphrase`

- 是否必填：是
- 修改后是否需要重启：是

创建 API Key 时由你自己设置的 API Passphrase。它不是 KuCoin 网站登录
密码，也不是资金密码。

本项目按 UTA 账户实现，内部固定使用 V3 请求签名。`API Key Version`
不是第四个凭证，也不需要写进 `config.yaml`；你只需要填写上面三个值。
启动时程序会访问 UTA 余额接口，并核对返回的账户类型是 `UNIFIED`。

#### `kucoin.site_type`

- 默认值：`global`
- 是否必填：否
- 修改后是否需要重启：是

KuCoin 站点区域。普通国际站保持：

```yaml
site_type: global
```

只有账户明确属于其他 KuCoin 地区域站点时才需要修改。

### `monitor`

控制后台自动查询频率和页面数据过期提示。

```yaml
monitor:
  interval_seconds: 60
  stale_after_seconds: 180
```

#### `monitor.interval_seconds`

- 默认值：`60`
- 代码允许的最小值：`10`
- 是否必填：否
- 修改后是否需要重启：是

后台查询间隔，单位是秒。`60` 表示大约每分钟查询一次：

- 现货账户余额；
- Futures 当前持仓；
- Futures 账户权益；
- 现货价格；
- 合约规格。

不建议为调试盲目设置得太小，否则会增加 KuCoin API 频率限制风险。

#### `monitor.stale_after_seconds`

- 默认值：`180`
- 代码允许的最小值：`30`
- 是否必填：否
- 修改后是否需要重启：是

最后一次成功快照距离当前时间超过多少秒后，页面显示“数据已过期”。

例如采集间隔为60秒，过期时间设为180秒，表示连续约三分钟没有成功获取
数据才显示过期。

“已过期”不代表仓位为零，只表示最近的数据可能已经不可靠。页面会继续
保留最后一次成功结果。

### `feishu`

使用飞书群自定义机器人的 Webhook 发送敞口报警：

```yaml
feishu:
  enabled: true
  webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/替换为真实地址"
  timeout_seconds: 8
```

- `enabled`：是否启用飞书报警，默认 `false`。
- `webhook_url`：飞书自定义机器人的 Webhook；只使用普通 Webhook，不需要
  App ID 或 App Secret。若机器人开启了签名校验，当前简单版本暂不支持。
- `timeout_seconds`：单次飞书请求超时，默认 8 秒，最小 1 秒。
- 修改这些配置后需要重启服务。

每次成功刷新后，程序会筛选状态不是“已对冲”的资产并发送报警。
`hedge.excluded_assets` 中的 KCS 等现货储备资产不会报警。一个币种持续存在
敞口时只报警一次；它恢复为“已对冲”后再次出现敞口，才会再次报警。服务
重启后会重新建立内存状态，因此当时仍存在的敞口会再报警一次。

飞书发送失败只会写入 `runtime_logs/kucoin_exposure.log`，不会把 KuCoin
采集标记为失败；下一轮刷新会继续尝试发送。真实 Webhook 只应保存在已被
Git 忽略的 `config.yaml` 中，不要填写到 `config.example.yaml`。

### 页面交易记录

页面底部的“最近交易记录（本页面）”读取本地 SQLite 中最近 20 次平仓操作，
展示大致时间、操作人、币种、处理数量、现货/合约下单结果和错误信息。
时间按浏览器本地时区显示为“月-日 时:分”，不显示年份和秒。

“清空记录”按钮会在二次确认后删除 SQLite 中的全部页面平仓操作记录。
该操作不会访问 KuCoin，也不会影响订单、余额或持仓，但删除的本地记录
无法恢复。接口要求有效登录状态和 CSRF 校验。

这部分不会额外请求 KuCoin，因此不会增加 API 限频或影响监控性能。记录只
包含通过本页面发起的双边平仓和全部平仓；其他服务器上的交易程序产生的
成交不在这里展示。SQLite 中仍保存完整操作结果，页面接口只返回用于列表
展示的简短摘要，避免把大段快照反复发送给浏览器。

### `hedge`

控制现货与合约如何匹配，以及页面怎样判断是否完成对冲。

```yaml
hedge:
  quote_currency: USDT
  matched_threshold_percent: 1
  warning_threshold_percent: 5
  dust_value_usdt: 1
```

#### `hedge.quote_currency`

- 默认值：`USDT`
- 是否必填：否
- 修改后是否需要重启：是

对冲监控的统一计价币种。第一版按以下市场设计：

- 现货：`BTC-USDT`、`ETH-USDT` 等；
- 合约：USDT结算的 Futures；
- 敞口价值：统一折算成 USDT。

第一版不建议修改成其他币种。

#### `hedge.matched_threshold_percent`

- 默认值：`1`
- 是否必填：否
- 修改后是否需要重启：是

偏差率不超过这个百分比时，页面显示“已对冲”。

偏差率计算方式：

```text
偏差率 =
abs(现货数量 + 带方向的合约币数量)
÷ max(abs(现货数量), abs(合约币数量))
× 100%
```

例如：

```text
现货：+1 BTC
合约：-0.995 BTC
净敞口：+0.005 BTC
偏差率：0.5%
```

在默认阈值 `1%` 下，它显示“已对冲”。

#### `hedge.warning_threshold_percent`

- 默认值：`5`
- 是否必填：否
- 修改后是否需要重启：是

偏差率高于“已对冲”阈值、但不超过该值时，页面显示“轻微偏多”或
“轻微偏空”。

默认规则：

```text
偏差率 <= 1%       已对冲
1% < 偏差率 <= 5% 轻微偏多或轻微偏空
偏差率 > 5%        未对冲偏多或未对冲偏空
现货和合约同方向   同向暴露
```

`warning_threshold_percent` 应大于等于
`matched_threshold_percent`。

#### `hedge.dust_value_usdt`

- 默认值：`1`
- 是否必填：否
- 修改后是否需要重启：是

净敞口折合价值低于这个金额时，将其作为无法交易或无需处理的灰尘余额，
页面按“已对冲”显示。

例如剩余 `0.20 USDT` 的 BTC，即使币数量不为零，也可能低于 KuCoin
最小下单金额，不能继续卖出。默认 `1` 表示这类净敞口不再触发未对冲
提示。

这个配置只影响对冲状态显示，不会把真实余额从现货余额表中删除。

#### `hedge.excluded_assets`

- 默认值：空列表
- 是否必填：否
- 修改后是否需要重启：是

用于配置只作为现货储备、不参与多空对冲的资产：

```yaml
hedge:
  excluded_assets:
    - KCS
```

被排除的资产仍会显示现货数量、可卖数量、当前价格和 USDT 价值，但偏差率
显示为 `—`，状态显示为“现货储备”。页面不会为它显示“双边平仓”按钮，
“全部平仓”也会跳过它。后端接口会再次校验该配置，因此不能通过直接调用
接口卖出被排除的资产。

### `symbols.aliases`

用于统一 KuCoin 现货和合约中不同的币种代码。

```yaml
symbols:
  aliases:
    XBT: BTC
```

KuCoin BTC 合约可能返回：

```text
baseCurrency = XBT
```

而现货余额返回：

```text
currency = BTC
```

配置 `XBT: BTC` 后，程序会把两者视为同一个资产，再进行对冲计算。

如果以后发现其他币种存在类似差异，可以继续添加：

```yaml
symbols:
  aliases:
    XBT: BTC
    合约代码: 现货代码
```

不要随意添加不确定的映射，错误映射会导致不相关资产被合并。

### `trading`

控制页面是否允许真实交易，以及双边平仓的执行方式。

```yaml
trading:
  enabled: false
  cancel_open_orders_before_close: true
  verification_delay_seconds: 1.5
```

#### `trading.enabled`

- 默认值：`false`
- 是否必填：否
- 修改后是否需要重启：是

这是本服务自己的真实交易总开关：

```yaml
enabled: false
```

表示：

- 定时查询正常运行；
- 页面正常展示；
- 双边平仓按钮禁用；
- 后端拒绝平仓请求；
- 不会真实撤单或下单。

改成：

```yaml
enabled: true
```

才允许真实双边平仓。

推荐首次使用顺序：

1. 保持 `false`；
2. 启动页面；
3. 查看页面中的余额、负债、合约张数、`multiplier` 及“换算校验”；
4. 所有合约显示“通过”后，改成 `true`；
5. 重启 KuCoin 独立服务；
6. 使用小仓位做第一次真实测试。

#### `trading.cancel_open_orders_before_close`

- 默认值：`true`
- 是否必填：否
- 修改后是否需要重启：是

双边平仓前，是否先撤销该币种对应的普通活动委托。

保持 `true` 时：

1. 撤销现货交易对的普通活动委托；
2. 撤销相关 Futures 合约的普通活动委托；
3. 任意撤单请求未确认时，停止本次双边平仓；
4. 撤单确认后重新查询余额和仓位；
5. 再提交双边市价单。

这样可以降低平仓后旧挂单再次成交、重新产生仓位的风险。

不建议设为 `false`。即使保持 `true`，KuCoin 的条件单、止损单也可能不
属于普通活动委托，平仓前后仍应在 KuCoin 页面确认。

#### `trading.verification_delay_seconds`

- 默认值：`1.5`
- 是否必填：否
- 修改后是否需要重启：是

双边市价单提交后等待多少秒，再重新查询现货余额和 Futures仓位。

等待时间过短时，KuCoin 的账户数据可能尚未完全更新，页面可能暂时显示
残余仓位；时间过长则会让用户等待更久。第一版建议保持 `1.5`。

它不是订单超时时间，也不会造成自动重复下单。

## multiplier换算

KuCoin Futures返回的是合约张数，不能直接与现货币数量比较。程序会访问
KuCoin 公共合约接口查询每个合约的 `multiplier`，这一步不需要 API Key：

```text
合约币数量 = 合约张数 × multiplier
```

例如：

```text
BTC现货：+1 BTC
XBTUSDTM空仓：1000张
multiplier：0.001 BTC/张
合约币数量：-1 BTC
假设标记价为 70,000 USDT：每张折合 70 USDT，1000 张折合 70,000 USDT
净敞口：0 BTC
```

多仓记为正数，空仓记为负数：

```text
净敞口数量 = 现货数量 + 合约币数量
净敞口价值 = 净敞口数量 × 当前价格
```

程序不是只把公式展示出来，还会自动做第二路校验：

```text
程序计算仓位价值 = |合约张数 × multiplier × 标记价格|
换算误差率 = |程序计算仓位价值 - KuCoin positionValue| / positionValue
```

误差不超过 `0.5%` 时页面显示“通过”。误差更大、`multiplier` 缺失或
无法得到有效仓位价值时显示“异常”，并在后端拒绝该币种的双边平仓，
防止现货腿已经卖出而合约数量口径错误。页面会同时展示 KuCoin 返回的
张数、每张面值、每张折合 USDT、换算币数、仓位折合 USDT 和误差率，
便于定位差异。

第一版主要支持 USDT 正向合约。识别到反向合约时会保留持仓展示，但不会
在未确认换算口径前把它加入自动对冲汇总。

## 双边平仓的实际行为

页面中每个币种有一个“双边平仓”按钮。以 BTC 为例，它表示：

- 市价卖出该子账户可交易 Spot账户里的 BTC；
- 对所有以 BTC/XBT 为基础币的 Futures仓位提交 reduce-only市价平仓；
- 两侧下单任务同时创建，但同一 API Key 的私有请求会按限频器串行发送；
- 最后重新查询两侧，并显示是否还有残余。

完整流程：

1. 用户点击按钮；
2. 页面从本地最新快照显示预计处理数量；
3. 用户确认；
4. 后端取得全局及币种操作锁，避免重复点击；
5. 后端重新查询最新现货余额和 Futures仓位；
6. 撤销普通现货和合约活动委托；
7. 撤单未全部确认时停止，不继续下单；
8. 重新查询可用现货和合约仓位；
9. 提交现货市价卖单和 Futures reduce-only平仓单；
10. 等待 `verification_delay_seconds`；
11. 重新查询并保存操作结果。

页面顶部还有“全部平仓”按钮。它会从本地最新快照取得全部非计价币资产，
然后逐个币种执行上述流程：

- 每个币种仍会在下单前重新查询实际余额和仓位；
- 一个币种失败后继续处理其他币种；
- 最终返回逐币种成功、失败和残余结果；
- 全部平仓与任意单币种平仓共用一个全局操作锁，不能并发或重复提交；
- `trading.enabled: false` 时按钮禁用，后端也拒绝请求。

逐币种处理是为了控制相同 API Key 的 REST 请求突发量，因此资产较多时
可能需要较长时间。实际交易程序仍在运行时，它也可能重新挂单或开仓。

网络超时后程序不会盲目重复下单。因为请求可能已经被 KuCoin 接收，只是
响应在网络中丢失。此时页面显示结果不确定，需要检查 KuCoin订单记录和
最新残余敞口。

## 可能无法完全平掉的情况

以下情况会留下残余余额，页面会显示“未完全平仓”：

- 现货在 `main`/funding账户，而不在可直接下单的 `trade`/`trade_hf`
  账户；
- 现货余额低于交易对的最小数量；
- 现货价值低于交易对最小下单金额；
- 数量精度取整后留下极小尾数；
- 市价保护导致订单只成交一部分；
- 一侧下单失败；
- 网络超时导致订单结果暂时无法确认；
- 条件单或止损单未被普通撤单接口清除；
- KuCoin账户或市场处于限制、结算、风控状态。

小额尾数属于正常交易规则限制。真实余额仍会显示在现货余额表中，而
`dust_value_usdt` 只决定它是否继续被标记为未对冲。

币数量在网页中最多显示 12 位小数，极小的非零数量使用科学计数法，避免
被格式化成 `0`。当净敞口价值低于 `dust_value_usdt`、状态按灰尘余额
显示为“已对冲”时，偏差率也统一显示为 `0%`，避免出现“已对冲但偏差
100%”的矛盾口径。

## 数据和日志

SQLite数据库：

```text
kucoin_exposure/runtime_data/kucoin_exposure.sqlite3
```

保存：

- 每次成功或失败的采集；
- 现货余额；
- Futures持仓；
- 每个币种的对冲结果；
- 双边平仓请求和结果。

运行日志：

```text
kucoin_exposure/runtime_logs/kucoin_exposure.log
```

日志不会主动输出 API Secret、API Passphrase 或完整签名请求头。

## 常见问题

### 页面能打开，但没有数据

检查：

- `api_key`、`api_secret`、`api_passphrase` 是否正确；
- 服务器时间是否准确；
- `runtime_logs/kucoin_exposure.log` 中的具体错误。

### 查询正常，但双边平仓按钮不可用

检查 `trading.enabled` 是否仍为 `false`，以及页面上的合约换算校验是否
全部通过。换算异常时程序会主动拒绝执行双边平仓。

### 出现 `429000: Too many requests`

这是 KuCoin UTA 的用户级限频。程序会串行发送私有 REST 请求，并在相邻
私有请求之间至少等待 `0.6` 秒。由于监控服务可能与另一台服务器上的实际
交易程序共用同一 UID/API Key，监控端遇到 `429000` 会立即让路，不自动
重试，并保留最后一次成功快照。日志会记录 `limit`、`remaining` 和
`reset_ms`。

程序会先查询对应交易对是否存在普通活动委托，没有委托就跳过
`cancel-all`。平仓预览只读取本地 SQLite 中的最新快照；用户最终确认后
才重新查询 KuCoin 并执行撤单、下单。这样能降低监控端的 REST 占用，但
使用相同 UID 时无法保证绝对零影响。

真实下单请求不会因为 `429000` 或网络超时自动重试，避免第一次请求实际
已成交但响应丢失时重复下单。如果安全重试全部失败，本次双边平仓会停止，
现货和合约下单均不会开始。若同一个 UTA UID 还有其他程序高频调用接口，
仍应降低其他程序的请求频率。

### 修改 `trading.enabled` 后按钮仍然不可用

配置在服务启动时读取。修改后需要重启 `python -m kucoin_exposure` 这个
独立进程，再刷新页面。

### 为什么现货总量和可卖现货不同

“现货总量”包含当前子账户中所有现货账户类型的余额，用来计算真实资产
敞口。“可卖现货”只统计可以直接通过 Spot订单卖出的交易账户可用余额。

例如资产仍在 main/funding账户时，它属于真实敞口，但不能直接由 Spot
卖单卖出，所以双边平仓后可能留下这部分余额。

### 为什么显示已对冲但仍有极小余额

该余额的 USDT价值可能低于 `dust_value_usdt`，或者低于 KuCoin交易对
最小下单量。页面仍会在现货余额表中显示真实数量，只是不再把它标记为
需要处理的敞口。

### 立即刷新

页面上的“立即刷新”会直接调用 KuCoin REST 接口重新查询余额、持仓、
交易对、价格和合约账户信息，不需要等待分钟级定时任务。页面平时每
30 秒读取一次本地最新快照，这种自动读取不会访问 KuCoin。

如果点击时后台刷新正在执行，手动刷新会先等待该轮查询完成，再执行
一轮新的 REST 查询，确保按钮返回的是点击后主动取得的数据。后台
定时任务遇到其他刷新正在执行时会跳过，避免请求堆积。
