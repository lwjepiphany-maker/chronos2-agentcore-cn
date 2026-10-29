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
LLM_BASE_URL="${LLM_BASE_URL:-https://api.ppinfra.com/v3/openai}"; LLM_MODEL="${LLM_MODEL:-deepseek/deepseek-v4-flash-0731}"
# Key 指纹（sha256 前 12 位，不含 Key 本身）：变化时模板会跑一次短构建把新 Key 写进 AgentCore Identity
LLM_KEY_FP=$(printf %s "${LLM_API_KEY}" | python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:12])')

# 部署前先用这把 Key 调一次大模型接口：模型名写错 / Key 无效 / 余额不足在这里就能发现，不用等 Agent 报 500
if [[ "${SKIP_LLM_CHECK:-0}" != "1" ]]; then
  echo "==> [0/3] 预检大模型接口 ${LLM_BASE_URL} 模型 ${LLM_MODEL}"
  code=$(curl -sS -m 60 -o /tmp/llm_check.json -w '%{http_code}' "${LLM_BASE_URL%/}/chat/completions" \
    -H "Authorization: Bearer ${LLM_API_KEY}" -H "Content-Type: application/json" \
    -d "{\"model\":\"${LLM_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":5}" || echo 000)
  if [[ "${code}" != "200" ]]; then
    echo "大模型接口预检失败（HTTP ${code}）："; cat /tmp/llm_check.json 2>/dev/null; echo
    echo "请核对 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY（接口返回的 message 通常会写明支持的模型名）。确认无误仍要继续可加 SKIP_LLM_CHECK=1"; exit 1
  fi
  echo "预检通过"
fi

PARAMS=(GitRepoUrl="${REPO_URL}" SourceS3Uri="${SRC_S3}" GitRef="${GIT_REF:-main}" GitHubToken="${GITHUB_TOKEN:-none}"
  ModelWeightsBaseUrl="${WEIGHTS_URL:-https://www.modelscope.cn/models/amazon/chronos-2/resolve/master}" ImageTag="${IMAGE_TAG:-v1}" RuntimeNamePrefix="${PREFIX:-chronos2}"
  LlmApiKey="${LLM_API_KEY}" LlmBaseUrl="${LLM_BASE_URL}" LlmModel="${LLM_MODEL}" StoreCity="${STORE_CITY:-西安}" IdleTimeoutSeconds="${IDLE_TIMEOUT:-600}")

# 2026-10-28 之前的模板把 ECR 仓库放在栈里；直接换成新模板会连镜像一起删。检测到旧栈时自动先跑一次"保留 ECR"的中间模板
if aws cloudformation describe-stack-resources --stack-name "${STACK}" \
     --query "StackResources[?ResourceType=='AWS::ECR::Repository'].LogicalResourceId" --output text 2>/dev/null | grep -q .; then
  echo "==> [1/3 前置] 检测到旧版栈（ECR 仓库仍是栈资源），先标记为保留（约 1 分钟），镜像不会被删"
  aws cloudformation deploy --stack-name "${STACK}" --template-file iac/migrate/step1-retain-ecr.yaml \
    --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset --parameter-overrides "${PARAMS[@]}"
fi

echo "==> [1/3] 部署 CloudFormation 栈 ${STACK}（首次含镜像构建约 25~40 分钟；镜像已存在时约 3 分钟）"
aws cloudformation deploy --stack-name "${STACK}" --template-file iac/template.yaml \
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset \
  --parameter-overrides "${PARAMS[@]}" LlmKeyFingerprint="${LLM_KEY_FP}" || {
  # 本机网络抖动会让 CLI 在等待阶段断开，但栈在云端继续；只要栈还在进行中就接着等，不要误判为部署失败
  st=$(aws cloudformation describe-stacks --stack-name "${STACK}" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)
  case "${st}" in
    CREATE_IN_PROGRESS) echo "CLI 连接中断，栈仍在创建中，继续等待…"; aws cloudformation wait stack-create-complete --stack-name "${STACK}" ;;
    UPDATE_IN_PROGRESS|UPDATE_COMPLETE_CLEANUP_IN_PROGRESS) echo "CLI 连接中断，栈仍在更新中，继续等待…"; aws cloudformation wait stack-update-complete --stack-name "${STACK}" ;;
    CREATE_COMPLETE|UPDATE_COMPLETE) echo "栈已完成（${st}）" ;;
    *) echo "部署失败，栈状态：${st}。排查：aws cloudformation describe-stack-events --stack-name ${STACK}；对照 docs/排障指南.md"; exit 1 ;;
  esac
}

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
换大模型 / 换 Key：改 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 重跑本脚本（第 2 个参数传 -），约 2~4 分钟，不重建镜像。详见 docs/优化与运维指南.md
删除：aws cloudformation delete-stack --stack-name ${STACK}   （数据桶保留，需手动删除）
EOF
