# 补货助手 Agent（Strands）+ Open-Meteo 天气 MCP；linux/arm64
# 构建上下文为仓库根目录：docker build -f app_agent/Dockerfile .
FROM public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5

# aws-opentelemetry-distro：AgentCore Observability 链路追踪（ADOT 自动注入，不改业务代码）
# 依赖只走境内镜像。不要加 pypi.org：pip 会选全局最新版本，导致从境外慢速下载
COPY app_agent/requirements.txt /tmp/requirements.txt
# 每次尝试只用一个 PyPI 镜像站（阿里云 → 华为云 → 中科大 → 清华），轮换重试：某一站抖动/返回空文件时不会连累整次安装
# 不要用 --extra-index-url 混合多站：pip 会随机从任一站取包，一站坏整次就失败
RUN for idx in https://mirrors.aliyun.com/pypi/simple https://repo.huaweicloud.com/repository/pypi/simple \
               https://mirrors.ustc.edu.cn/pypi/simple https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple; do \
      echo "==> pip via $idx" && pip install --index-url "$idx" -r /tmp/requirements.txt && break; \
      echo "pip via $idx failed, trying next mirror in 15s"; sleep 15; false; \
    done

COPY app_agent/agent.py app_agent/weather_mcp.py app_agent/batch.py app_agent/factors.py app_agent/master.py /app/
RUN useradd -m -u 1000 app
USER app
WORKDIR /app
EXPOSE 8080
# 用 opentelemetry-instrument 启动：Agent / LLM / 工具 / boto3 的 span 自动上报到 CloudWatch
CMD ["opentelemetry-instrument", "python", "/app/agent.py"]
