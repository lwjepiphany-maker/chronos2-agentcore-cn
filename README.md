# Chronos-2 销量预测 × Amazon Bedrock AgentCore（中国区）

便利店销量预测与补货助手：Amazon Chronos-2 时序模型以 MCP Server 形式运行在 AgentCore Runtime（CPU），
补货助手 Agent（Strands Agents SDK + 国产大模型）同样托管在 Runtime，结合 Open-Meteo 天气、Code Interpreter 精确计算和图表。
2026 年 10 月在 AWS 中国（宁夏）区域验证通过。

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
| `docs/` | 客户版部署方案、技术指南、两阶段验证报告 |

## 部署（约 25 分钟）

前提：中国区账号已开通 Bedrock AgentCore；AWS CLI v2；一个 OpenAI 兼容大模型的 API Key。
**不需要本机 Docker**，镜像由 CodeBuild 在区域内构建。

```bash
python iac/prepare_data.py 门店销售数据.xlsx data/sales_daily.parquet
LLM_API_KEY='sk_xxx' bash iac/deploy.sh https://github.com/<you>/chronos2-agentcore-cn.git data/sales_daily.parquet
```

可选环境变量：`WEIGHTS_URL`（模型权重的 GitHub Release 地址，默认从 ModelScope 下载）、`STORE_CITY`（默认西安）、
`LLM_BASE_URL` / `LLM_MODEL`、`IMAGE_TAG`（改动后重新部署即滚动升级）、`GITHUB_TOKEN`（私有仓库）。

模型权重（约 480 MB）不放在 git 里。两种来源：
1. 默认：CodeBuild 从 ModelScope `amazon/chronos-2` 下载（境内，快）
2. 可选：放到本仓库的 GitHub Release（`model.safetensors`、`config.json`、`configuration.json` 三个文件），把 Release 的下载地址前缀传给 `WEIGHTS_URL`

## 使用

```bash
# 前端（本机）
pip install -r frontend/requirements.txt
streamlit run frontend/app.py -- --agent-arn <AgentRuntimeArn> --region cn-northwest-1

# 业务系统直接调用
aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <AgentRuntimeArn> --qualifier DEFAULT \
  --runtime-session-id "$(uuidgen)-$(uuidgen)" --runtime-user-id ops-001 \
  --content-type application/json --accept application/json \
  --payload "$(echo -n '{"prompt":"预测5号店商品12080084下周销量"}' | base64)" /dev/stdout

# 分析师用 Claude Code 直连预测服务（命令见栈输出 McpProxyCommand）
```

调用方 IAM 需要对 Runtime ARN 及 `ARN/*` 的 `bedrock-agentcore:InvokeAgentRuntime`，前端还需读数据桶 `charts/*`。
**调 Agent 必须传 `runtimeUserId`**，否则 Agent 取不到大模型 Key。

## 费用（宁夏区）

按会话计费：一次 3 轮对话约 0.15 元（其中大模型约 0.10 元）；POC 10 人 × 5 次/天约 175 元/月；无人使用时接近零。详见 `docs/部署方案-客户版.md`。

## 中国区注意

Runtime 入站只有 IAM 或非 Cognito 的 OIDC；无 AgentCore Memory；大模型需用国产模型 API；对话内容会发给模型服务商，需评估合规。
