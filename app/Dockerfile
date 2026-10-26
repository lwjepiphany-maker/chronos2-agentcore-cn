# Chronos-2 预测 MCP Server：AgentCore Runtime 要求 linux/arm64；CPU 推理
# 构建上下文为仓库根目录：docker build -f app/Dockerfile .
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5 MODEL_DIR=/opt/model

# torch 固定 CPU 版（+cpu，避免 CUDA 依赖把镜像撑过 2GB），从境内的 PyTorch 轮子镜像取（阿里云主、上交大备）；
# download.pytorch.org 跨境下载 159MB 在晚高峰可能只有几十 kB/s 甚至断流，不要直接用。
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --index-url https://mirrors.aliyun.com/pypi/simple \
      -f https://mirrors.aliyun.com/pytorch-wheels/cpu/ -f https://mirror.sjtu.edu.cn/pytorch-wheels/cpu/ torch==2.14.1+cpu && \
    pip install --index-url https://mirrors.aliyun.com/pypi/simple --extra-index-url https://mirrors.cloud.tencent.com/pypi/simple -r /tmp/requirements.txt

# 模型权重打进镜像（ModelScope amazon/chronos-2），运行时不依赖外网
COPY model/ /opt/model/
COPY app/server.py /app/server.py

RUN useradd -m -u 1000 app
USER app
EXPOSE 8000
CMD ["python", "/app/server.py"]
