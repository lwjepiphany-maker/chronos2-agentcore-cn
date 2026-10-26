# 销量预测 Agent 上云：AgentCore 宁夏区验证报告（第二阶段）

> 验证日期：2026-10-12 · 区域：cn-northwest-1（宁夏） · 账号：<ACCOUNT_ID>（内部测试账号）
> 前置：第一阶段已验证 Chronos-2 以 MCP Server 形式运行在 AgentCore Runtime（见《验证报告-方案B-AgentCore.md》）

## 一、结论

**Agent 本身也可以托管在宁夏区 AgentCore 上，全链路已跑通**：

```
用户 → [IAM SigV4] → Runtime①：Agent（Strands Agents SDK，HTTP 协议）
                        ├─ LLM：PPIO 平台 DeepSeek V4 Flash（OpenAI 兼容接口，API Key 托管在 AgentCore Identity）
                        ├─ Runtime②：Chronos-2 预测 MCP（SigV4，区域内调用 0.2–0.4 s）
                        └─ S3：门店销售历史（parquet，私有桶）
```

- 三轮真实对话（单品预测 → Top3 批量预测+补货建议 → 追问历史明细）全部正确，具备会话记忆
- 一次 3 轮对话总成本约 **¥0.15**（LLM 约 ¥0.10，两个 Runtime 约 ¥0.05）
- 全部资源 IAM 鉴权，匿名访问 403，不对公网开放
- 顺带验证了 4 个此前"待验证"项：Identity API Key 托管 ✅、Runtime VPC 模式 ✅、Gateway 接 MCP Server ✅、DeepSeek 工具调用稳定性 ✅

## 二、用到的 AgentCore 组件及验证状态

| 组件 | 用途 | 状态 |
|---|---|---|
| Runtime①（Agent，HTTP 协议） | 托管 Agent 代码 | ✅ 已部署，`<AGENT_RUNTIME_ID>` |
| Runtime②（Chronos-2，MCP 协议） | 预测服务 | ✅ 第一阶段已部署，Agent 复用 |
| Identity · API Key credential provider | 托管 PPIO API Key，Agent 运行时按需获取（0.14 s） | ✅ 可用，provider `ppio-deepseek` |
| Gateway（AWS_IAM 入站，mcpServer 目标） | 把 Runtime② 挂为 Gateway 工具 | ✅ 可用，每次调用约 3 s（直连 1.1 s） |
| Runtime VPC 网络模式 | 让 Agent 访问 VPC 内数据库 | ✅ 私有子网 + NAT 可用；默认 VPC 公有子网会 CREATE_FAILED |
| Observability | 结构化日志 ✅；链路追踪需在镜像里加 ADOT 自动注入 | 部分启用 |
| Memory | — | 中国区不可用，会话内记忆靠 Runtime 进程（每会话一个 microVM） |

## 三、测试结果（云上 Runtime，客户端在本地 Mac）

| 轮次 | 问题 | 工具调用 | token（输入/输出） | 端到端耗时 |
|---|---|---|---|---|
| 1 | 预测5号店商品12080084下周销量 | recent_sales ×1、forecast_sku ×1 | 2.7k / 0.5k | 13.5 s（含冷启动约 3.5 s） |
| 2 | 5号店近30天 Top3 商品，分别预测下周销量并给补货建议 | list_top_skus ×1、recent_sales ×2、forecast_sku ×3 | 12.5k / 1.9k | 8.8 s |
| 3 | 刚才第一个商品最近两周每天卖多少？（考记忆） | recent_sales ×1 | 18.7k / 2.3k | 2.9 s |

- 耗时主要在 LLM 生成（第 2 轮输出 1,882 token 的 Markdown 表格）；Chronos 预测在区域内只要 0.2–0.4 s
- 第 3 轮正确记住"第一个商品"是 12070007，说明单会话内记忆有效
- 输入 token 约 55% 命中 PPIO 的缓存（cache read 价格是正常输入的 1/3）
- 本地与云上输出一致（相同的预测数字、相近的措辞）

### 关键设计：LLM 不搬运数据

`forecast_sku(store, sku, days)` 工具在内部查历史、调 Chronos、只把 7 行预测 + 汇总返回给 LLM。对比第一阶段"客户端把历史数组传给 MCP"的方式，3 个商品的预测只用了 12.5k 输入 token。

## 四、费用估算（人民币）

**单价**（PPIO 官网 2026-10-12；AgentCore 中国区官网）：

| 项目 | 单价 |
|---|---|
| DeepSeek V4 Flash 输入 / 缓存命中 / 输出 | ¥3 / ¥1 / ¥9 每百万 token |
| Runtime CPU / 内存 | ¥0.8669 / vCPU-h，¥0.1148 / GB-h |

**一次对话（3 轮，实测 token）**：

| 项目 | 计算 | 费用 |
|---|---|---|
| LLM 输入（未命中缓存 14.1k） | × ¥3/M | ¥0.042 |
| LLM 输入（命中缓存 19.8k） | × ¥1/M | ¥0.020 |
| LLM 输出（4.6k） | × ¥9/M | ¥0.042 |
| Runtime①（Agent，约 1 GB × 11 min，CPU 大部分在等 LLM） | | ≈ ¥0.03 |
| Runtime②（Chronos，约 1.5 GB × 6 min） | | ≈ ¥0.02 |
| **合计** | | **≈ ¥0.15 / 次对话** |

**月度估算**（22 个工作日）：

| 场景 | 对话次数/月 | LLM | Runtime ×2 | 合计 |
|---|---|---|---|---|
| POC：10 人 × 5 次/天 | 1,100 | ≈ ¥115 | ≈ ¥55 | **≈ ¥170** |
| 试运行：50 人 × 10 次/天 | 11,000 | ≈ ¥1,150 | ≈ ¥550 | **≈ ¥1,700** |
| 每日全量跑批（9,507 组合，不经 LLM） | — | 0 | ≈ ¥6 | ¥6 |
| 固定费用：ECR 镜像 1 GB + Identity + 日志 | — | — | — | < ¥5 |

对比原方案 GPU 端点 ¥1,150–3,740/月的固定开销：POC 量级下云上 Agent 方案约为其 5–15%，且无会话时接近零成本。LLM 占总成本约 2/3，是后续优化重点（缩短回复、少输出表格、换 V4.1 Flash 可再降 30% 以上）。

## 五、踩过的坑（迁移时必须注意）

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | Agent 取 API Key 时报 `CreateWorkloadIdentity` AccessDenied | 用 SigV4 调 Runtime 时，**必须带 `runtimeUserId`**（HTTP 头 `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`），Runtime 才会注入 workload access token；否则 SDK 走"本地开发"分支 | 调用方传 `runtimeUserId` |
| 2 | `GetResourceApiKey` AccessDenied | 该操作的授权资源是 **workload-identity 和 workload-identity-directory**，不是 token-vault | IAM 加 `workload-identity-directory/default` 和 `.../workload-identity/<runtime 名>-*` |
| 3 | 取 secret AccessDenied | Identity 把 API Key 存在 Secrets Manager，名字以 `bedrock-agentcore-identity!default/apikey/` 开头，执行角色需要 `secretsmanager:GetSecretValue` | IAM 加该 secret 的 ARN 前缀 |
| 4 | VPC 模式 CREATE_FAILED，无错误原因 | 放在默认 VPC 的公有子网，microVM 无公网 IP，拉不到镜像 | 用**私有子网 + NAT**（或 ECR/S3/logs 的 VPC 终端节点） |
| 5 | Gateway mcpServer 目标创建报 `IamCredentialProvider is required` | IAM 出站鉴权必须显式给 `{"service":"bedrock-agentcore","region":"cn-northwest-1"}` | 见 infra 命令 |
| 6 | 执行角色的 `workload-identity` 名字规则 | 自动创建为 `<runtime 名>-<id>`，IAM 里用 `chronos2_agent_poc-*` 通配 | — |

最终的执行角色策略见 `infra/agent-permissions.json`（10 条语句，全部最小化到具体资源）。

## 六、与"Agent 在客户端"方案的对比

| 维度 | Agent 在客户端（第一阶段） | Agent 在云上（本阶段） |
|---|---|---|
| 用户是否需要 AWS 凭证 | 需要（每人配 mcp-proxy-for-aws） | 不需要，由前端后端服务统一用 IAM 调 Agent |
| LLM | 用户自带（Claude/Kiro） | 云上统一接 DeepSeek 等国产模型 |
| 数据接入 | 用户/客户端自己准备历史数据 | Agent 直接读 S3/数据库，一句话直达 |
| 可观测/审计 | 分散在各客户端 | 集中在 CloudWatch |
| 适合 | 内部数据分析师试用 | 门店运营人员、企业微信/钉钉接入 |

**推荐**：面向门店运营人员推广时采用云上 Agent；数据分析师可继续用客户端直连 Chronos。

## 七、生产化还需补的

1. **用户入口鉴权**：目前是 IAM；给业务人员用需要前端后端服务（企业微信/钉钉机器人）持有 IAM 凭证调用，或接客户的 OIDC IdP 走 CUSTOM_JWT（宁夏无 Cognito）
2. **链路追踪**：镜像加 `aws-opentelemetry-distro`，用 `opentelemetry-instrument` 启动，即可在 CloudWatch 看每轮的 LLM/工具耗时
3. **数据接入**：parquet 换成 Athena 查 S3 或直连客户 ERP（后者用 VPC 模式）
4. **跨会话记忆**：如需"上次问过什么"，用 DynamoDB 存对话历史（中国区无 AgentCore Memory）
5. **合规确认**：对话内容（含门店代码、商品代码、销量）会发送给 PPIO 第三方模型，需客户确认；如不允许则改 SageMaker 自托管模型

## 八、本阶段新建的资源（均带 `Project=chronos2-agentcore-verify`）

| 资源 | 标识 | 闲置成本 |
|---|---|---|
| Runtime（Agent） | `<AGENT_RUNTIME_ID>` | 0 |
| Runtime（VPC 模式验证用） | `<VPC_CHECK_RUNTIME_ID>` | 0，可删 |
| Gateway + 目标 | `<GATEWAY_ID>` / target `Q9LZLK3GNO` | 0（按调用计费） |
| Identity API Key provider | `ppio-deepseek`（含 Secrets Manager 一个 secret） | Secrets Manager 约 ¥2.8/月 |
| ECR 仓库 | `chronos2-agent-poc:v1`（187 MB） | 约 ¥0.13/月 |
| IAM 角色 | `Chronos2AgentPocRole`、`Chronos2GatewayPocRole` | 0 |
| S3 对象 | `s3://<DATA_BUCKET>/chronos2-agent-poc/data/sales_daily.parquet`（1 MB） | ≈ 0 |
| 日志组 | `/aws/bedrock-agentcore/runtimes/chronos2_agent_poc-*`、`chronos2_vpc_check*` | ≈ 0 |

## 九、文件清单

| 文件 | 说明 |
|---|---|
| `app_agent/agent.py` | Agent 代码（Strands + OpenAIModel + 3 个取数/预测工具 + Identity 取 key） |
| `Dockerfile.agent` | Agent 镜像（187 MB） |
| `infra/agent-permissions.json` | Agent 执行角色最小权限（含踩坑后补的 3 条） |
| `tests/agent_cloud_test.py` | 云上 3 轮对话测试 |
| `results_agent_cloud.json` | 云上测试结果 |

---

## 十、增补（2026-10-26）：Observability 链路追踪 + Code Interpreter

### 10.1 Observability

- 镜像加入 `aws-opentelemetry-distro`，启动命令改为 `opentelemetry-instrument python agent.py`，业务代码零改动；Runtime 环境变量加 `OTEL_RESOURCE_ATTRIBUTES=service.name=chronos2_agent_poc`。
- **坑**：span 导出一直报 `400 Bad Request`。原因是账号未开启 CloudWatch **Transaction Search**（X-Ray 的 OTLP 端点只在 Trace 目的地为 CloudWatchLogs 时接收 span）。宁夏区可开，步骤：`logs put-resource-policy`（允许 xray.amazonaws.com 写 `aws/spans`）→ `xray update-trace-segment-destination --destination CloudWatchLogs`（约 6 分钟变 ACTIVE）→ `xray update-indexing-rule` 设采样 100%。
- 开启后一次请求的 trace（14.8 s，32 个 span）可以看到：4 轮 Strands 事件循环，每轮 LLM 调用（api.ppinfra.com）0.3~0.7 s 网络 + 1.7~3.9 s 生成，工具 list_top_skus 218 ms、forecast_sku 405 ms、run_python 1,086 ms（含 Code Interpreter 两次调用 794 + 123 ms）。耗时 83% 在 LLM 生成。
- 执行角色需增加 `xray:PutSpans`、`xray:PutSpansForIndexing`，以及对 `aws/spans` 日志组的写权限。

### 10.2 Code Interpreter

- 中国区系统解释器 `aws.codeinterpreter.v1` 可用，会话启动约 2 s，执行一段 Python 约 2 s。
- 新增两个工具：`run_python`（精确计算：补货量 = P90 − 库存 − 在途，按箱规向上取整）、`plot_forecast`（matplotlib 画历史 + 预测区间图，`readFiles` 读回 PNG 后存私有 S3）。
- **坑**：`readFiles` 返回的二进制在 SDK 1.24 里是 bytes 的 repr 字符串（`"b'\x89PNG...'"`），不是 base64，直接 b64decode 得到 5 字节垃圾；用 `ast.literal_eval` 还原。`readFiles` 的路径必须是沙箱工作目录内的相对路径，`/tmp/x.png` 会被拒绝。
- 沙箱没有中文字体，图内文字用英文。
- 云上三轮测试：预测 → "库存 120、一箱 12、按 P90 订几箱"（run_python 算出 23 箱 276 件，与手算一致）→ "画成图"（52 KB PNG 落到 S3）。端到端分别 18.0 s / 5.1 s / 6.2 s。

### 10.3 本次新增 / 变更的资源

| 资源 | 说明 |
|---|---|
| 账号级：X-Ray Trace 目的地 → CloudWatchLogs（Transaction Search） | 影响整个账号 cn-northwest-1，span 按 CloudWatch Logs 写入计费（¥6.228/GB，免费 5GB） |
| 账号级：CloudWatch Logs 资源策略 `TransactionSearchXRayAccess` | 允许 X-Ray 写 `aws/spans` |
| Agent Runtime 版本 2（镜像 `chronos2-agent-poc:v2`，200 MB） | — |
| S3 前缀 `chronos2-agent-poc/charts/` | Agent 生成的图表 |

## 十一、增补（2026-10-26）：Open-Meteo 天气 MCP + 带天气协变量的预测

- 新增 `app_agent/weather_mcp.py`：自写的 FastMCP（stdio）服务，封装 Open-Meteo 免费 API（无需 Key）。工具 `geocode(city)`、`daily_weather(city, start, end)`；日期在今天之后走预报（最多 16 天），之前走历史实况 archive，可混合。WMO 天气代码映射为与客户数据一致的中文类别（晴/多云/阴/小雨/中雨/大雨…）。
- 在 Agent 容器内作为子进程运行，Strands `MCPClient(stdio_client)` 接入，天气工具直接暴露给 LLM，可回答"下周西安天气"。
- 新增工具 `forecast_sku_with_weather`：历史天气取自客户数据的天气/气温列，未来天气取自 Open-Meteo，连同星期数一起作为协变量调用 Chronos `forecast_with_covariates`，并同时返回不带天气的基线。
- **门店城市推断**：客户数据没有城市字段。用数据里 2025-03~05 的日最高/最低温与 Open-Meteo 历史实况比对，西安相关系数 0.99、平均误差 1℃（郑州 0.95/2.2℃，北京 0.84/4.2℃），确认门店在西安，`DEFAULT_CITY=西安`。
- **数据覆盖修正**：客户数据实际是两段各 85 天（2025 与 2026 年的 3-01~5-24），不是连续 450 天。`_daily_series` 改为只在最后一段连续窗口内补 0，避免把中间 9 个月空档当成零销量。此前的基准测试用的是最近 30/60 天，都在第二段窗口内，结果不受影响。
- 云上测试（Runtime v3）：

| 问题 | 工具 | 端到端 |
|---|---|---|
| 下周西安天气怎么样？ | daily_weather（archive，2.5 s） | 26 s（含冷启动） |
| 考虑天气的话 5 号店 12080084 下周销量，和不考虑比有何差别 | forecast_sku_with_weather（6.9 s：取天气 + 2 次 Chronos） | 17.7 s |

  5/25~5/31 西安前三天有雨。带天气 P50 合计 172.5，基线 182.4（-5%），差异最大的是周六（21.6 vs 29.1）。该周真实销量数据集中没有，无法评估哪个更准；历史回测中协变量版 WAPE 0.42 vs 单变量 0.38（单品）、Top100 批量 0.29，天气对单品精度的贡献需要更多样本验证。
- Runtime PUBLIC 模式可出站访问 api.open-meteo.com（宁夏 → 境外，archive 接口 2.5~6 s，forecast 接口 2.7 s）；生产若用 VPC 模式需 NAT。
