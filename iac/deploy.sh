#!/usr/bin/env bash
# 一键部署：CloudFormation 建栈（含 CodeBuild 构建镜像）→ 上传销售数据 → 打印调用信息
# 用法：LLM_API_KEY=sk_xxx bash iac/deploy.sh <GitRepoUrl 或 s3://bucket/src.zip> <sales_daily.parquet 或 -> [stack-name]
#       第 2 个参数传 "-" 表示不上传数据（换 Key / 换模型 / 升级代码时用，避免覆盖 S3 上更新的数据）
# 可选环境变量：AWS_REGION(默认 cn-northwest-1) GIT_REF(main) WEIGHTS_URL IMAGE_TAG(v1) PREFIX(chronos2) STORE_CITY(西安)
#               LLM_BASE_URL LLM_MODEL GITHUB_TOKEN(私有仓库)
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:?GitRepoUrl 或 s3://bucket/src.zip}"; DATA_FILE="${2:?parquet 文件路径}"; STACK="${3:-chronos2-agentcore}"
# 源码来源：GitHub 地址，或离线交付时的 S3 zip（zip 内容为仓库根目录）
if [[ "${SRC}" == s3://* ]]; then REPO_URL=""; SRC_S3="${SRC}"; else REPO_URL="${SRC}"; SRC_S3=""; fi
: "${LLM_API_KEY:?请通过环境变量 LLM_API_KEY 传入大模型 API Key}"
export AWS_REGION="${AWS_REGION:-cn-northwest-1}" AWS_DEFAULT_REGION="${AWS_REGION:-cn-northwest-1}"

echo "==> [1/3] 部署 CloudFormation 栈 ${STACK}（含镜像构建，首次约 20~30 分钟）"
aws cloudformation deploy --stack-name "${STACK}" --template-file iac/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides \
    GitRepoUrl="${REPO_URL}" SourceS3Uri="${SRC_S3}" GitRef="${GIT_REF:-main}" GitHubToken="${GITHUB_TOKEN:-none}" \
    ModelWeightsBaseUrl="${WEIGHTS_URL:-https://www.modelscope.cn/models/amazon/chronos-2/resolve/master}" ImageTag="${IMAGE_TAG:-v1}" RuntimeNamePrefix="${PREFIX:-chronos2}" \
    LlmApiKey="${LLM_API_KEY}" LlmBaseUrl="${LLM_BASE_URL:-https://api.ppinfra.com/v3/openai}" \
    LlmModel="${LLM_MODEL:-deepseek/deepseek-v4-flash-0731}" StoreCity="${STORE_CITY:-西安}" \
    IdleTimeoutSeconds="${IDLE_TIMEOUT:-600}"

out() { aws cloudformation describe-stacks --stack-name "${STACK}" --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
BUCKET=$(out DataBucket)

if [[ "${DATA_FILE}" == "-" ]]; then
  echo "==> [2/3] 跳过数据上传（保留 s3://${BUCKET}/data/sales_daily.parquet 现有数据）"
else
  echo "==> [2/3] 上传销售数据到 s3://${BUCKET}/data/sales_daily.parquet"
  aws s3 cp "${DATA_FILE}" "s3://${BUCKET}/data/sales_daily.parquet" --only-show-errors
fi

echo "==> [3/3] 完成"
cat <<EOF

预测服务 Runtime ARN : $(out ChronosRuntimeArn)
Agent Runtime ARN    : $(out AgentRuntimeArn)
数据桶               : ${BUCKET}

验证：
  python tests/bench.py --runtime-arn "$(out ChronosRuntimeArn)" --repeat 2
  python tests/agent_cloud_test2.py "$(out AgentRuntimeArn)"
前端（本机）：
  pip install -r frontend/requirements.txt
  streamlit run frontend/app.py -- --agent-arn "$(out AgentRuntimeArn)" --region ${AWS_REGION}
Claude Code 直连预测服务：
  $(out McpProxyCommand)

更新代码：改 IMAGE_TAG 重新运行本脚本，第 2 个参数传 -（只重建改动过的镜像，两个 Runtime 滚动到新版本）。
换大模型：改 LLM_BASE_URL / LLM_MODEL 重跑（约 2 分钟）；换 API Key 还需同时改 IMAGE_TAG（约 8 分钟）。详见 docs/优化与运维指南.md
删除：aws cloudformation delete-stack --stack-name ${STACK}   （数据桶保留，需手动删除）
EOF
