<#
.SYNOPSIS
  Windows 版一键部署（PowerShell 5.1 / 7+）：CloudFormation 建栈（含 CodeBuild 构建镜像）→ 上传销售数据 → 打印调用信息。
  与 iac/deploy.sh 等价。

.EXAMPLE
  $env:LLM_API_KEY = 'sk_xxx'
  .\iac\deploy.ps1 -Source https://github.com/lwjepiphany-maker/chronos2-agentcore-cn.git -DataFile data\sales_daily.parquet

  可选参数 / 环境变量：-StackName（默认 chronos2-agentcore）-Region（cn-northwest-1）-Prefix（chronos2）-ImageTag（v1）
  -GitRef（main）-StoreCity（西安）-LlmBaseUrl -LlmModel -WeightsUrl -IdleTimeout（600）-GitHubToken
  也可以用 Git for Windows 自带的 Git Bash 直接运行 iac/deploy.sh。
#>
param(
  [Parameter(Mandatory = $true)] [string] $Source,      # GitHub 仓库地址，或 s3://bucket/src.zip
  [Parameter(Mandatory = $true)] [string] $DataFile,    # prepare_data.py 生成的 parquet；传 "-" 表示不上传数据
  [string] $StackName  = "chronos2-agentcore",
  [string] $Region     = $(if ($env:AWS_REGION) { $env:AWS_REGION } else { "cn-northwest-1" }),
  [string] $Prefix     = "chronos2",
  [string] $ImageTag   = "v1",
  [string] $GitRef     = "main",
  [string] $StoreCity  = "西安",
  [string] $LlmBaseUrl = "https://api.ppinfra.com/v3/openai",
  [string] $LlmModel   = "deepseek/deepseek-v4-flash-0731",
  [string] $WeightsUrl = "https://www.modelscope.cn/models/amazon/chronos-2/resolve/master",
  [int]    $IdleTimeout = 600,
  [string] $GitHubToken = "none"
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not $env:LLM_API_KEY) { throw "请先设置环境变量 LLM_API_KEY（大模型 API Key）：`$env:LLM_API_KEY = 'sk_xxx'" }
if ($DataFile -ne "-" -and -not (Test-Path $DataFile)) { throw "找不到数据文件 $DataFile，请先运行 python iac\prepare_data.py" }
$env:AWS_DEFAULT_REGION = $Region
# Key 指纹（sha256 前 12 位，不含 Key 本身）：变化时模板会跑一次短构建把新 Key 写进 AgentCore Identity
$sha = [System.Security.Cryptography.SHA256]::Create()
$KeyFp = (($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($env:LLM_API_KEY)) | ForEach-Object { $_.ToString("x2") }) -join "").Substring(0, 12)

# 部署前先用这把 Key 调一次大模型接口，模型名 / Key / 余额问题在这里就能发现
if ($env:SKIP_LLM_CHECK -ne "1") {
  Write-Host "==> [0/3] 预检大模型接口 $LlmBaseUrl 模型 $LlmModel"
  try {
    $body = @{ model = $LlmModel; messages = @(@{ role = "user"; content = "hi" }); max_tokens = 5 } | ConvertTo-Json -Depth 5 -Compress
    Invoke-RestMethod -Method Post -Uri ($LlmBaseUrl.TrimEnd("/") + "/chat/completions") -TimeoutSec 60 `
      -Headers @{ Authorization = "Bearer $($env:LLM_API_KEY)" } -ContentType "application/json; charset=utf-8" `
      -Body ([Text.Encoding]::UTF8.GetBytes($body)) | Out-Null
    Write-Host "预检通过"
  } catch {
    $detail = $_.ErrorDetails.Message; if (-not $detail) { $detail = $_.Exception.Message }
    throw "大模型接口预检失败：$detail`n请核对 -LlmBaseUrl / -LlmModel / LLM_API_KEY。确认无误仍要继续可设置 `$env:SKIP_LLM_CHECK = '1'"
  }
}

if ($Source -like "s3://*") { $RepoUrl = ""; $SrcS3 = $Source } else { $RepoUrl = $Source; $SrcS3 = "" }

Write-Host "==> [1/3] 部署 CloudFormation 栈 $StackName（含镜像构建，首次约 20~30 分钟）"
aws cloudformation deploy --stack-name $StackName --template-file iac/template.yaml `
  --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset --region $Region `
  --parameter-overrides `
    "GitRepoUrl=$RepoUrl" "SourceS3Uri=$SrcS3" "GitRef=$GitRef" "GitHubToken=$GitHubToken" `
    "ModelWeightsBaseUrl=$WeightsUrl" "ImageTag=$ImageTag" "RuntimeNamePrefix=$Prefix" `
    "LlmApiKey=$($env:LLM_API_KEY)" "LlmKeyFingerprint=$KeyFp" "LlmBaseUrl=$LlmBaseUrl" "LlmModel=$LlmModel" "StoreCity=$StoreCity" `
    "IdleTimeoutSeconds=$IdleTimeout"
if ($LASTEXITCODE -ne 0) { throw "CloudFormation 部署失败，请查看：aws cloudformation describe-stack-events --stack-name $StackName --region $Region" }

function Get-Output([string] $Key) {
  aws cloudformation describe-stacks --stack-name $StackName --region $Region `
    --query "Stacks[0].Outputs[?OutputKey=='$Key'].OutputValue" --output text
}
$Bucket = Get-Output "DataBucket"

if ($DataFile -eq "-") {
  Write-Host "==> [2/3] 跳过数据上传（保留 S3 上现有数据）"
} else {
  Write-Host "==> [2/3] 上传销售数据到 s3://$Bucket/data/sales_daily.parquet"
  aws s3 cp $DataFile "s3://$Bucket/data/sales_daily.parquet" --region $Region --only-show-errors
  if ($LASTEXITCODE -ne 0) { throw "数据上传失败" }
}

$ChronosArn = Get-Output "ChronosRuntimeArn"
$AgentArn   = Get-Output "AgentRuntimeArn"
$McpCmd     = Get-Output "McpProxyCommand"

Write-Host "==> [3/3] 完成"
Write-Host ""
Write-Host "预测服务 Runtime ARN : $ChronosArn"
Write-Host "Agent Runtime ARN    : $AgentArn"
Write-Host "数据桶               : $Bucket"
Write-Host ""
Write-Host "验证："
Write-Host "  python tests\agent_cloud_test2.py $AgentArn"
Write-Host "模拟商品主数据（客户暂无真实数据时）："
Write-Host "  python tools\mock_master_data.py --parquet $DataFile --out data\master --upload s3://$Bucket/master/"
Write-Host "前端（本机）："
Write-Host "  pip install -i https://mirrors.aliyun.com/pypi/simple -r frontend\requirements.txt"
Write-Host "  streamlit run frontend\app.py -- --agent-arn $AgentArn --data-bucket $Bucket --region $Region"
Write-Host "Claude Code 直连预测服务："
Write-Host "  $McpCmd"
Write-Host ""
Write-Host "更新代码：改 -ImageTag 重新运行本脚本（-DataFile -）。换大模型/换 Key：改参数重跑即可。删除：aws cloudformation delete-stack --stack-name $StackName --region $Region（数据桶与 ECR 镜像保留）"
