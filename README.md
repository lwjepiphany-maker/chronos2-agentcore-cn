# Chronos-2 销量预测 × Amazon Bedrock AgentCore（中国区）

便利店销量预测与补货助手：Amazon Chronos-2 时序模型以 MCP Server 形式运行在 AgentCore Runtime（CPU），
补货助手 Agent（Strands Agents SDK + 国产大模型）同样托管在 Runtime，结合 Open-Meteo 天气、Code Interpreter 精确计算和图表。
2026 年 10 月在 AWS 中国（宁夏）区域验证通过。

![架构图](docs/architecture/chronos2-agentcore.png)

交互式版本（可点节点看源码引用、切换深浅主题、导出图片）：[docs/architecture/chronos2-agentcore.html](docs/architecture/chronos2-agentcore.html)（下载后用浏览器打开）。图由 [Archify](https://github.com/tt-a1i/archify) 基于本仓库代码与 `iac/template.yaml` 生成，每个节点附源码行号。

```
业务系统 / 前端 ──IAM──▶ Runtime①：补货助手 Agent ──▶ 大模型 API（OpenAI 兼容）
                              ├──▶ Runtime②：Chronos-2 预测 MCP（CPU）
                              ├──▶ Open-Meteo 天气 MCP（容器内）
                              ├──▶ AgentCore Code Interpreter（计算 / 画图）
                              ├──▶ AgentCore Identity（大模型 Key）
                              └──▶ Amazon S3（销售历史 parquet、图表）
AI 助手（Claude Code / Kiro）──mcp-proxy-for-aws──▶ Runtime②（分析师直连）
```

## 目录

| 路径 | 内容 |
|---|---|
| `iac/template.yaml` | **一个 CloudFormation 模板部署全部**：CodeBuild 从本仓库构建两个 arm64 镜像 → ECR → 两个 AgentCore Runtime、IAM 角色、数据桶、Identity 密钥、Transaction Search |
| `iac/deploy.sh` | 一键脚本：建栈 → 上传数据 → 打印调用方式 |
| `iac/prepare_data.py` | 销售 Excel → parquet |
| `app/` | Chronos-2 预测 MCP Server（`server.py`、`Dockerfile`） |
| `app_agent/` | 补货助手 Agent（`agent.py`）、天气 MCP（`weather_mcp.py`）、`Dockerfile` |
| `frontend/app.py` | Streamlit 聊天前端（本机 / 内网运行） |
| `tests/` | 预测服务基准、Agent 对话测试、测试用例生成 |
| `docs/` | 客户版部署方案、技术指南、两阶段验证报告、`architecture/` 架构图（PNG + 交互式 HTML） |

## 部署（约 25 分钟）

前提：中国区账号已开通 Bedrock AgentCore；AWS CLI v2；一个 OpenAI 兼容大模型的 API Key。
**不需要本机 Docker**，镜像由 CodeBuild 在区域内构建。

```bash
python iac/prepare_data.py 门店销售数据.xlsx data/sales_daily.parquet
LLM_API_KEY='sk_xxx' bash iac/deploy.sh https://github.com/<you>/chronos2-agentcore-cn.git data/sales_daily.parquet
```

可选环境变量：`WEIGHTS_URL`（模型权重下载地址前缀，默认 ModelScope 直链；也可指向 GitHub Release）、`STORE_CITY`（默认西安）、
`LLM_BASE_URL` / `LLM_MODEL`、`IMAGE_TAG`（改动后重新部署即滚动升级）、`GITHUB_TOKEN`（私有仓库）。

模型权重（约 480 MB）不放在 git 里。两种来源：
1. 默认：CodeBuild 从 ModelScope `amazon/chronos-2` 直链下载（境内，快）
2. 可选：放到本仓库的 GitHub Release（`model.safetensors`、`config.json`、`configuration.json` 三个文件），把 Release 的下载地址前缀传给 `WEIGHTS_URL`

## 使用

### 1. 前端（门店运营人员，本机或内网）

```bash
pip install -r frontend/requirements.txt
streamlit run frontend/app.py -- --agent-arn <AgentRuntimeArn> --region cn-northwest-1
```

### 2. 业务系统直接调用 Agent

```bash
aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <AgentRuntimeArn> --qualifier DEFAULT \
  --runtime-session-id "$(uuidgen)-$(uuidgen)" --runtime-user-id ops-001 \
  --content-type application/json --accept application/json \
  --payload "$(echo -n '{"prompt":"预测5号店商品12080084下周销量"}' | base64)" /dev/stdout
```

| 参数 | 含义 | 怎么取值 |
|---|---|---|
| `--runtime-session-id` | 会话标识。同一个 ID 连续调用 = 同一次对话，Agent 记住上下文；换 ID = 新会话 | 调用方自己生成的任意字符串，**至少 33 个字符**。示例用 `uuidgen`（macOS/Linux 自带的随机 UUID 命令）拼两个凑长度；业务系统里通常用"用户 ID + 会话序号"或数据库里的会话主键 |
| `--runtime-user-id` | 调用用户的标识，**必填**（Runtime 据此给 Agent 注入身份令牌，Agent 才能从 Identity 取到大模型 Key） | 业务系统的用户名或工号，如 `store05-zhang` |
| `--payload` | 请求体 JSON `{"prompt": "问题"}`，CLI 要求 base64 编码；用 SDK（boto3 等）直接传字节即可 | 见 `docs/技术部署指南.md` 的 Python 示例 |
| `/dev/stdout` | 响应输出位置 | 返回 JSON 里 `result` 是回答正文，`tools` / `usage` / `total_ms` 是本轮工具调用、token 和耗时 |

### 3. 分析师用 Claude Code / Kiro 直连预测服务

不经过 Agent，直接把 Chronos-2 的三个预测工具挂进 AI 助手，适合自己准备好历史数据做分析的人。

```bash
# 前提：本机已安装 Claude Code 和 uv（提供 uvx），AWS 凭证对预测服务 ARN 有 bedrock-agentcore:InvokeAgentRuntime 权限
# 命令在栈输出 McpProxyCommand 里已经拼好，复制执行即可，形如：
claude mcp add --scope user chronos2-forecast -- uvx mcp-proxy-for-aws@latest \
  'https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/runtimes/<URL编码的预测服务ARN>/invocations?qualifier=DEFAULT' \
  --service bedrock-agentcore --region cn-northwest-1
```

之后在 Claude Code 里直接提问，例如："用 chronos2-forecast 预测这组日销量未来 7 天：[30, 28, 35, …]"，助手会调用 `forecast` / `batch_forecast` / `forecast_with_covariates`。`mcp-proxy-for-aws` 在本机用 AWS 凭证做 SigV4 签名后转发，所以不需要密码，也没有公网入口。Kiro / Cursor 的 MCP 配置里把同一条命令写成 `command: uvx`、`args: [...]` 即可。

## 费用（宁夏区）

下面是**整套方案的全部费用**，不只是 AgentCore（单价来自中国区官网，用量为实测）：

| 项目 | 计费方式 | 一次 3 轮对话 | 说明 |
|---|---|---|---|
| AgentCore Runtime ×2（Agent + 预测服务） | 按会话实际 vCPU / 内存按秒计费 | ≈ ¥0.05 | 无会话时为 0 |
| AgentCore Code Interpreter | 同上（¥0.608/vCPU-h，¥0.064/GB-h） | ≈ ¥0.02 | 只在算补货量 / 画图时启动 |
| 大模型（DeepSeek V4 Flash @ PPIO） | 按 token | ≈ ¥0.10 | 占总成本约 2/3，可换模型 |
| AgentCore Identity | 经 Runtime 调用免费 | 0 | — |
| Open-Meteo 天气 | 免费 | 0 | — |
| **合计** | | **≈ ¥0.17** | |

| 月度（22 个工作日） | AgentCore 部分 | 大模型 | 合计 |
|---|---|---|---|
| POC：10 人 × 5 次/天 | ≈ ¥80 | ≈ ¥115 | **≈ ¥195** |
| 试运行：50 人 × 10 次/天 | ≈ ¥800 | ≈ ¥1,150 | **≈ ¥1,950** |
| 每日全量跑批（约 9,500 组合，不经大模型） | ≈ ¥6 | 0 | ≈ ¥6 |

固定费用：ECR 两个镜像约 1 GB（≈ ¥0.7/月）、Secrets Manager 1 个密文（≈ ¥2.8/月）、S3 数据与图表（< ¥1/月）、CloudWatch 日志与 Trace（免费额度内）。每次部署 / 升级一次 CodeBuild 构建约 10 分钟，约几元。对比原方案 GPU 端点常驻 ¥1,150～3,740/月。

## 中国区注意

Runtime 入站只有 IAM 或非 Cognito 的 OIDC；无 AgentCore Memory；大模型需用国产模型 API；对话内容会发给模型服务商，需评估合规。
