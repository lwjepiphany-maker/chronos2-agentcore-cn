# Chronos-2 预测 MCP Server：AgentCore Runtime 要求 linux/arm64；CPU 推理
# 构建上下文为仓库根目录：docker build -f app/Dockerfile .
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5 MODEL_DIR=/opt/model

# torch 固定 CPU 版（+cpu，避免 CUDA 依赖把镜像撑过 2GB），从境内的 PyTorch 轮子镜像取（阿里云主、上交大备）；
# download.pytorch.org 跨境下载 159MB 在晚高峰可能只有几十 kB/s 甚至断流，不要直接用。
COPY app/requirements.txt /tmp/requirements.txt
# 每次尝试只用一个 PyPI 镜像站（阿里云 → 华为云 → 中科大 → 清华），轮换重试：某一站抖动/返回空文件时不会连累整次安装
# 不要用 --extra-index-url 混合多站：pip 会随机从任一站取包，一站坏整次就失败
RUN for idx in https://mirrors.aliyun.com/pypi/simple https://repo.huaweicloud.com/repository/pypi/simple \
               https://mirrors.ustc.edu.cn/pypi/simple https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple; do \
      echo "==> pip via $idx" && \
      pip install --index-url "$idx" \
        -f https://mirrors.aliyun.com/pytorch-wheels/cpu/ -f https://mirror.sjtu.edu.cn/pytorch-wheels/cpu/ torch==2.14.1+cpu && \
      pip install --index-url "$idx" -r /tmp/requirements.txt && break; \
      echo "pip via $idx failed, trying next mirror in 15s"; sleep 15; false; \
    done

# 模型权重打进镜像（ModelScope amazon/chronos-2），运行时不依赖外网
COPY model/ /opt/model/
COPY app/server.py /app/server.py

RUN useradd -m -u 1000 app
USER app
EXPOSE 8000
CMD ["python", "/app/server.py"]
