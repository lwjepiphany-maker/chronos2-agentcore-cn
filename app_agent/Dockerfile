# 补货助手 Agent（Strands）+ Open-Meteo 天气 MCP；linux/arm64
# 构建上下文为仓库根目录：docker build -f app_agent/Dockerfile .
FROM public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# aws-opentelemetry-distro：AgentCore Observability 链路追踪（ADOT 自动注入，不改业务代码）
COPY app_agent/requirements.txt /tmp/requirements.txt
RUN pip install -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -r /tmp/requirements.txt

COPY app_agent/agent.py app_agent/weather_mcp.py /app/
RUN useradd -m -u 1000 app
USER app
WORKDIR /app
EXPOSE 8080
# 用 opentelemetry-instrument 启动：Agent / LLM / 工具 / boto3 的 span 自动上报到 CloudWatch
CMD ["opentelemetry-instrument", "python", "/app/agent.py"]
