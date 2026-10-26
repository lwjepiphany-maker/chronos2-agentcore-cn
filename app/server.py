"""Chronos-2 MCP Server for Amazon Bedrock AgentCore Runtime (China regions, CPU).

Plan B: Chronos-2 runs on CPU inside the AgentCore Runtime microVM, replacing
API Gateway + Lambda + SageMaker GPU endpoint of the original POC.

Tool names / parameters / response format are identical to
chronos2-cn-交付包/mcp-server/lambda_function.py, so MCP clients need no changes.
Inference logic is the same as chronos2-cn-交付包/deploy/inference.py.

AgentCore Runtime MCP contract: listen on 0.0.0.0:8000, path /mcp,
stateless streamable HTTP, linux/arm64 image.
"""
import logging
import os
import threading
import time

import numpy as np
import torch
from chronos import Chronos2Pipeline
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chronos2-mcp")

MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/model")
DEFAULT_QUANTILE_LEVELS = [0.1, 0.5, 0.9]

# 每一天便利店 POC 实际使用的协变量集合（与 lambda_function.py 保持一致）
KNOWN_COVARIATE_KEYS = (
    "store_type, weather, temp_low, temp_high, weekday_num, holiday_num, "
    "has_promotion, promo_qty, promo_discount, price, brand, abc_class, promo_category"
)

_t0 = time.time()
pipeline = Chronos2Pipeline.from_pretrained(MODEL_DIR, device_map="cpu", dtype=torch.float32)
logger.info(f"Chronos-2 loaded on cpu from {MODEL_DIR} in {time.time() - _t0:.2f}s, "
            f"torch threads={torch.get_num_threads()}")

# json_response=True：返回普通 JSON 而非 SSE 流，经过 AgentCore 代理更稳定
mcp = FastMCP("chronos2-forecast", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)


# --- 推理逻辑（同 inference.py） ---

def _infer_dtype(values):
    return object if len(values) > 0 and isinstance(values[0], str) else np.float32


def _build_entry(item):
    entry = {"target": np.array(item["target"], dtype=np.float32)}
    for key in ("past_covariates", "future_covariates"):
        cov = item.get(key)
        if cov:
            entry[key] = {k: np.array(v, dtype=_infer_dtype(v)) for k, v in cov.items()}
    return entry


_predict_lock = threading.Lock()


def _predict(inputs, prediction_length, quantile_levels):
    with _predict_lock:
        return _predict_unlocked(inputs, prediction_length, quantile_levels)


def _predict_unlocked(inputs, prediction_length, quantile_levels):
    t0 = time.time()
    entries = [_build_entry(item) for item in inputs]
    quantiles, means = pipeline.predict_quantiles(
        entries, prediction_length=prediction_length, quantile_levels=quantile_levels
    )
    predictions = []
    for q_tensor, mean_tensor in zip(quantiles, means):
        q_arr = q_tensor[0].numpy()
        result = {"mean": mean_tensor[0].numpy().round(6).tolist()}
        for i, q in enumerate(quantile_levels):
            result[str(q)] = q_arr[:, i].round(6).tolist()
        predictions.append(result)
    logger.info(f"predict n_series={len(inputs)} horizon={prediction_length} "
                f"latency_ms={(time.time() - t0) * 1000:.0f}")
    return predictions


# --- MCP 工具（名称/参数/返回与 lambda_function.py 一致） ---

@mcp.tool()
def forecast(history: list[float], prediction_length: int = 7,
             quantile_levels: list[float] | None = None) -> dict:
    """预测时序数据的未来值。输入历史观测值（如过去90天日销量），返回未来N天的点预测和概率区间。

    Args:
        history: 历史观测值列表
        prediction_length: 预测未来步数（天数）
        quantile_levels: 分位数级别，默认 [0.1, 0.5, 0.9]
    """
    return _predict([{"target": history}], prediction_length,
                    quantile_levels or DEFAULT_QUANTILE_LEVELS)[0]


@mcp.tool()
def batch_forecast(time_series: list[list[float]], prediction_length: int = 7) -> list[dict]:
    """批量预测多条时序数据。一次请求发送多个SKU的历史数据，返回各自预测结果。

    Args:
        time_series: 多条时序历史数据
        prediction_length: 预测步数
    """
    return _predict([{"target": ts} for ts in time_series], prediction_length,
                    DEFAULT_QUANTILE_LEVELS)


@mcp.tool(description=(
    "带协变量（价格、促销、节假日等）的时序预测，利用外部因素提高精度。"
    "past_covariates: 历史协变量，key为特征名，value为数组（长度=history），数值型传数字数组，类别型传字符串数组。"
    f"本项目已验证的特征：{KNOWN_COVARIATE_KEYS}，可传子集。"
    "future_covariates: 未来已知协变量（长度=prediction_length），key需与past_covariates一致。"
))
def forecast_with_covariates(history: list[float], past_covariates: dict,
                             future_covariates: dict, prediction_length: int = 7) -> dict:
    return _predict(
        [{"target": history, "past_covariates": past_covariates,
          "future_covariates": future_covariates}],
        prediction_length, DEFAULT_QUANTILE_LEVELS,
    )[0]


def _warmup():
    """microVM 中镜像层按需加载：首次推理时才随机读取 480MB 权重，实测首调 ~28s。
    启动后在后台线程顺序预读权重进 page cache，再跑一次预热推理；
    服务端口立即可用，initialize/tools/list 不受影响，真实请求若早于预热完成则在锁上等待。"""
    with _predict_lock:
        t0 = time.time()
        with open(os.path.join(MODEL_DIR, "model.safetensors"), "rb") as f:
            while f.read(16 << 20):
                pass
        t1 = time.time()
        _predict_unlocked([{"target": [float(i % 7) for i in range(30)]}], 7, DEFAULT_QUANTILE_LEVELS)
        logger.info(f"warmup: read_weights={t1 - t0:.2f}s first_predict={time.time() - t1:.2f}s")


if __name__ == "__main__":
    threading.Thread(target=_warmup, daemon=True).start()
    mcp.run(transport="streamable-http")
