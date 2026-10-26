# 补货助手 Agent（Strands）+ Open-Meteo 天气 MCP；linux/arm64
# 构建上下文为仓库根目录：docker build -f app_agent/Dockerfile .
FROM public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5

# aws-opentelemetry-distro：AgentCore Observability 链路追踪（ADOT 自动注入，不改业务代码）
# 依赖只走境内镜像（阿里云主、腾讯云备）。不要加 pypi.org：pip 会选全局最新版本，导致从境外慢速下载
COPY app_agent/requirements.txt /tmp/requirements.txt
RUN pip install --index-url https://mirrors.aliyun.com/pypi/simple --extra-index-url https://mirrors.cloud.tencent.com/pypi/simple -r /tmp/requirements.txt

COPY app_agent/agent.py app_agent/weather_mcp.py /app/
RUN useradd -m -u 1000 app
USER app
WORKDIR /app
EXPOSE 8080
# 用 opentelemetry-instrument 启动：Agent / LLM / 工具 / boto3 的 span 自动上报到 CloudWatch
CMD ["opentelemetry-instrument", "python", "/app/agent.py"]
