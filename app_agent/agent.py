"""销量预测 Agent，运行在 Amazon Bedrock AgentCore Runtime（HTTP 协议）。

架构：
  用户 → Runtime①(本 Agent, Strands) → LLM(PPIO DeepSeek, OpenAI 兼容接口)
                                     → Runtime②(Chronos-2 MCP, SigV4) 做预测
                                     → S3 parquet 读历史销量

设计要点：
- 工具 forecast_sku 在内部取数并调用 Chronos，LLM 只拿到结论，不搬运历史销量数组。
- LLM API Key 通过 AgentCore Identity 的 API Key credential provider 获取（本地调试可用 PPIO_API_KEY 环境变量）。
- Runtime 每个会话一个 microVM，进程内的 Agent 对象即会话记忆（中国区无 AgentCore Memory）。
"""
import ast
import base64
import io
import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import uuid

import boto3
import pandas as pd
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batch  # noqa: E402  批量预测共享逻辑（与 tools/batch_forecast.py 共用）
import factors  # noqa: E402  节假日 / 促销 / 价格 / 天气协变量构造 + 回测指标
import master  # noqa: E402  商品主数据 / 库存快照 + 保质期补货计算
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent, tool
from strands.models.openai import OpenAIModel
from strands.tools.mcp import MCPClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("chronos2-agent")
os.environ.setdefault("TZ", "Asia/Shanghai")   # 报表、图表文件名用北京时间
time.tzset()

REGION = os.environ.get("AWS_REGION", "cn-northwest-1")
CHRONOS_RUNTIME_ARN = os.environ["CHRONOS_RUNTIME_ARN"]
DATA_URI = os.environ.get("DATA_URI", "data/sales_daily.parquet")  # s3://bucket/key 或本地路径
API_KEY_PROVIDER = os.environ.get("API_KEY_PROVIDER", "ppio-deepseek")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.ppinfra.com/v3/openai")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash-0731")
# Code Interpreter 生成的图表存到这个私有桶前缀（不走预签名 URL，前端用 IAM 读取）
CHART_BUCKET = os.environ.get("CHART_BUCKET", "")
CHART_PREFIX = os.environ.get("CHART_PREFIX", "chronos2-agent-poc/charts/")
REPORT_PREFIX = os.environ.get("REPORT_PREFIX", "reports/")   # 批量预测 Excel 报表，与图表同一个桶
MASTER_PREFIX = os.environ.get("MASTER_PREFIX", "master/")     # 商品主数据 products.parquet / 库存 inventory.parquet
MASTER = master.Master(CHART_BUCKET, MASTER_PREFIX, REGION) if CHART_BUCKET else None

app = BedrockAgentCoreApp()


# --- 数据：启动时整表读入内存（481k 行 / 约 5MB parquet），按门店+商品过滤 ---

def _load_sales() -> pd.DataFrame:
    t0 = time.time()
    if DATA_URI.startswith("s3://"):
        bucket, key = DATA_URI[5:].split("/", 1)
        body = boto3.client("s3", region_name=REGION).get_object(Bucket=bucket, Key=key)["Body"].read()
        df = pd.read_parquet(io.BytesIO(body))
    else:
        df = pd.read_parquet(DATA_URI)
    logger.info(f"sales data loaded rows={len(df)} range={df.date.min().date()}~{df.date.max().date()} "
                f"in {time.time() - t0:.2f}s")
    return df


def _latest_window(dates: pd.Series) -> pd.DatetimeIndex:
    """客户数据是分段的（如 2025 春季 + 2026 春季），只取最后一段连续日期，避免把中间的空档补成 0 销量。"""
    d = pd.Series(sorted(dates.unique()))
    breaks = d[d.diff() != pd.Timedelta(days=1)].index
    return pd.DatetimeIndex(d.iloc[breaks[-1]:].values)


class _Data:
    """销售数据延迟加载 + 热更新：
    - 数据文件可能在 Runtime 创建之后才上传（IaC 先建栈再传数据），启动时读不到不能让进程退出；
    - 前端重新上传 Excel 后，用 S3 对象的 ETag 判断是否变化（最多每 30 s 查一次 HEAD），变了就重新加载，不用等新会话。"""
    df: pd.DataFrame | None = None
    last_date: pd.Timestamp | None = None
    days: pd.DatetimeIndex | None = None
    etag: str | None = None
    checked_at: float = 0.0
    _lock = threading.Lock()
    CHECK_INTERVAL = 30

    @classmethod
    def _remote_etag(cls) -> str | None:
        if not DATA_URI.startswith("s3://"):
            return None
        bucket, key = DATA_URI[5:].split("/", 1)
        return boto3.client("s3", region_name=REGION).head_object(Bucket=bucket, Key=key)["ETag"]

    @classmethod
    def ensure(cls) -> pd.DataFrame:
        with cls._lock:
            now = time.time()
            stale = cls.df is None
            if not stale and now - cls.checked_at > cls.CHECK_INTERVAL:
                try:
                    stale = cls._remote_etag() != cls.etag
                except Exception as e:  # noqa: BLE001  检查失败不影响已加载的数据
                    logger.warning(f"etag check failed: {e}")
                cls.checked_at = now
            if stale:
                try:
                    df = _load_sales()
                    cls.etag = cls._remote_etag()
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(f"销售数据未就位，请先上传 {DATA_URI}（{type(e).__name__}）") from e
                cls.df, cls.last_date, cls.days, cls.checked_at = df, df.date.max(), _latest_window(df.date), now
                logger.info(f"sales data (re)loaded etag={cls.etag} window {cls.days[0].date()} ~ {cls.days[-1].date()} ({len(cls.days)} days)")
            return cls.df


try:
    _Data.ensure()
except Exception as e:  # noqa: BLE001  数据未就位时不退出，工具调用时再试
    logger.warning(f"sales data not loaded at startup ({e}); will retry on first tool call")
# 门店所在城市（用于取天气）。按客户数据里的历史气温与 Open-Meteo 实况比对，西安匹配度最高（相关 0.99，误差 1℃）
STORE_CITY = json.loads(os.environ.get("STORE_CITY_JSON", "{}"))
DEFAULT_CITY = os.environ.get("DEFAULT_CITY", "西安")


def _daily_series(store: int, sku: int, n_days: int) -> pd.Series:
    """门店-商品的日销量（窗口内无销售记录的日期补 0），取最近 n_days 天（不超过最后一段连续窗口）。"""
    SALES = _Data.ensure()
    s = (SALES[(SALES.store == store) & (SALES.sku == sku)]
         .groupby("date")["qty"].sum().reindex(_Data.days, fill_value=0.0))
    return s.iloc[-n_days:]


def _store_weather(store: int, index: pd.DatetimeIndex) -> pd.DataFrame:
    """门店级逐日天气（来自销售数据的天气/气温列），缺失日前向填充。"""
    SALES = _Data.ensure()
    w = (SALES[SALES.store == store].drop_duplicates("date").set_index("date")[["weather", "temp_low", "temp_high"]]
         .reindex(index).ffill().bfill())
    return w


def _last_date() -> pd.Timestamp:
    _Data.ensure()
    return _Data.last_date


# --- Chronos-2 MCP（Runtime②），用本 Runtime 执行角色的凭证做 SigV4 ---

def _chronos_url():
    return (f"https://bedrock-agentcore.{REGION}.amazonaws.com.cn/runtimes/"
            f"{urllib.parse.quote(CHRONOS_RUNTIME_ARN, safe='')}/invocations?qualifier=DEFAULT")


chronos = MCPClient(lambda: aws_iam_streamablehttp_client(
    endpoint=_chronos_url(), aws_region=REGION, aws_service="bedrock-agentcore"), startup_timeout=60)


def _chronos_call(tool: str, arguments: dict) -> dict:
    t0 = time.time()
    res = chronos.call_tool_sync(tool_use_id=f"fc-{uuid.uuid4().hex[:8]}", name=tool, arguments=arguments)
    if res["status"] != "success":
        raise RuntimeError(f"chronos error: {res['content']}")
    out = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    out = out.get("result", out)
    logger.info(f"chronos {tool} days={arguments.get('prediction_length')} latency_ms={(time.time() - t0) * 1000:.0f}")
    return out


def _chronos_forecast(history: list[float], days: int) -> dict:
    return _chronos_call("forecast", {"history": history, "prediction_length": days})


# --- Open-Meteo 天气 MCP（stdio 子进程，见 weather_mcp.py），为预测提供未来天气协变量 ---

from mcp import StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

WEATHER_MCP = os.environ.get("WEATHER_MCP_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "weather_mcp.py"))
weather = MCPClient(lambda: stdio_client(StdioServerParameters(command=sys.executable, args=[WEATHER_MCP])),
                    startup_timeout=60)


def _weather_days(city: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    res = weather.call_tool_sync(tool_use_id=f"wx-{uuid.uuid4().hex[:8]}", name="daily_weather",
                                 arguments={"city": city, "start_date": start.strftime("%Y-%m-%d"),
                                            "end_date": end.strftime("%Y-%m-%d")})
    if res["status"] != "success":
        raise RuntimeError(f"weather error: {res['content']}")
    out = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    return out.get("result", out)["days"]


# --- 暴露给 LLM 的工具（返回精简 JSON，不返回原始长数组） ---

@tool
def forecast_sku(store: int, sku: int, days: int = 7, history_days: int = 60) -> dict:
    """预测某门店某商品未来 days 天的日销量，并给出补货建议。

    Args:
        store: 门店代码（1~6）
        sku: 商品代码
        days: 预测天数，默认 7
        history_days: 用多少天历史做预测，默认 60
    """
    hist = _daily_series(store, sku, history_days)
    if hist.sum() == 0:
        return {"error": f"门店 {store} 商品 {sku} 最近 {history_days} 天没有销售记录"}
    fc = _chronos_forecast(hist.tolist(), days)
    dates = pd.date_range(_last_date() + pd.Timedelta(days=1), periods=days, freq="D")
    rows = [{"date": d.strftime("%Y-%m-%d"), "weekday": "一二三四五六日"[d.weekday()],
             "p10": round(fc["0.1"][i], 1), "p50": round(fc["0.5"][i], 1), "p90": round(fc["0.9"][i], 1)}
            for i, d in enumerate(dates)]
    return {
        "store": store, "sku": sku, "data_last_date": _last_date().strftime("%Y-%m-%d"),
        "history_summary": {"days": history_days, "total": round(float(hist.sum()), 1),
                            "daily_avg": round(float(hist.mean()), 2),
                            "last_7_days": [round(float(x), 1) for x in hist.iloc[-7:]]},
        "forecast": rows,
        "suggestion": {"expected_total_p50": round(sum(r["p50"] for r in rows), 1),
                       "safety_total_p90": round(sum(r["p90"] for r in rows), 1)},
    }


@tool
def forecast_sku_with_weather(store: int, sku: int, days: int = 7, history_days: int = 60,
                              city: str | None = None) -> dict:
    """带天气协变量的销量预测：用 Open-Meteo 取门店所在城市未来 days 天的天气/气温，连同历史天气一起输入 Chronos-2，
    并与不带天气的基线预测对比。用户关心天气影响、或问"下周下雨销量会怎样"时调用。

    Args:
        store: 门店代码（1~6）
        sku: 商品代码
        days: 预测天数，默认 7
        history_days: 历史窗口天数，默认 60
        city: 城市名，默认取门店配置（西安）
    """
    city = city or STORE_CITY.get(str(store), DEFAULT_CITY)
    hist = _daily_series(store, sku, history_days)
    if hist.sum() == 0:
        return {"error": f"门店 {store} 商品 {sku} 最近 {history_days} 天没有销售记录"}
    past_w = _store_weather(store, hist.index)
    fut_idx = pd.date_range(_last_date() + pd.Timedelta(days=1), periods=days, freq="D")
    fut_w = _weather_days(city, fut_idx[0], fut_idx[-1])
    if len(fut_w) < days:
        return {"error": f"只拿到 {len(fut_w)} 天天气（Open-Meteo 预报最多未来 16 天）"}
    past_cov = {"weather": past_w.weather.astype(str).tolist(),
                "temp_low": past_w.temp_low.astype(float).tolist(), "temp_high": past_w.temp_high.astype(float).tolist(),
                "weekday_num": [float(d.weekday() + 1) for d in hist.index]}
    fut_cov = {"weather": [w["weather"] or "多云" for w in fut_w],
               "temp_low": [float(w["temp_low"]) for w in fut_w], "temp_high": [float(w["temp_high"]) for w in fut_w],
               "weekday_num": [float(d.weekday() + 1) for d in fut_idx]}
    fc = _chronos_call("forecast_with_covariates", {"history": hist.tolist(), "past_covariates": past_cov,
                                                    "future_covariates": fut_cov, "prediction_length": days})
    base = _chronos_forecast(hist.tolist(), days)
    rows = [{"date": d.strftime("%Y-%m-%d"), "weekday": "一二三四五六日"[d.weekday()],
             "weather": fut_cov["weather"][i], "temp": f"{fut_cov['temp_low'][i]:.0f}~{fut_cov['temp_high'][i]:.0f}℃",
             "p10": round(fc["0.1"][i], 1), "p50": round(fc["0.5"][i], 1), "p90": round(fc["0.9"][i], 1),
             "baseline_p50": round(base["0.5"][i], 1)} for i, d in enumerate(fut_idx)]
    return {"store": store, "sku": sku, "city": city, "data_last_date": _last_date().strftime("%Y-%m-%d"),
            "covariates_used": ["weather", "temp_low", "temp_high", "weekday_num"],
            "forecast": rows,
            "totals": {"with_weather_p50": round(sum(fc["0.5"]), 1), "with_weather_p90": round(sum(fc["0.9"]), 1),
                       "baseline_p50": round(sum(base["0.5"]), 1)}}


@tool
def forecast_sku_with_factors(store: int, sku: int, days: int = 7, history_days: int = 60,
                              promo_days: list[str] | None = None, promo_type: str = "促销价",
                              discount_per_unit: float | None = None, use_weather: bool = True) -> dict:
    """带业务因素的预测：节假日（离线日历自动）、促销计划（用户给出的促销日期/类型/单件优惠）、价格、天气、星期
    一起作为协变量输入 Chronos-2，并与不带因素的基线对比。用户提到"促销 / 活动 / 节假日 / 五一 / 国庆"等场景时调用；
    对比"促销 vs 不促销"时调用两次（一次传 promo_days，一次不传）。

    Args:
        store: 门店代码
        sku: 商品代码
        days: 预测天数，默认 7
        history_days: 历史窗口，默认 60
        promo_days: 未来促销日期列表，如 ["2026-05-29","2026-05-30"]。不传 = 自动：若该商品历史上大部分时间在促销，
            则默认未来继续促销（很多商品长期挂促销价）；传空列表 [] = 明确未来不促销（用于算"停止促销"的影响）
        promo_type: 促销类型：促销价 / 换购 / 折扣 / 立减 / 赠品，默认取该商品历史最常见类型
        discount_per_unit: 单件优惠金额（元），不传则用该商品历史促销的平均值
        use_weather: 是否取 Open-Meteo 未来天气作为协变量，默认是
    """
    df = _Data.ensure()
    hist = _daily_series(store, sku, history_days)
    if hist.sum() == 0:
        return {"error": f"门店 {store} 商品 {sku} 最近 {history_days} 天没有销售记录"}
    fut_idx = pd.date_range(_last_date() + pd.Timedelta(days=1), periods=days, freq="D")
    past = factors.past_covariates(df, store, sku, hist.index)
    wx = None
    if use_weather and "weather" in past:
        try:
            wx = _weather_days(STORE_CITY.get(str(store), DEFAULT_CITY), fut_idx[0], fut_idx[-1])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"weather unavailable, continuing without forecast weather: {e}")
    promo_share = (sum(past["has_promotion"]) / len(past["has_promotion"])) if "has_promotion" in past else 0.0
    hist_types = [t for t in past.get("promo_type", []) if t != "无促销"]
    if hist_types and promo_type == "促销价":
        promo_type = max(set(hist_types), key=hist_types.count)   # 历史最常见的促销类型
    if promo_days is None:
        promo_mode = "沿用历史：继续促销" if promo_share >= 0.5 else "沿用历史：无促销"
        promo_days = [d.strftime("%Y-%m-%d") for d in fut_idx] if promo_share >= 0.5 else []
    else:
        promo_mode = "用户指定" if promo_days else "明确无促销"
    fut = factors.future_covariates(past, fut_idx, promo_days, promo_type, discount_per_unit, wx)
    fc = _chronos_call("forecast_with_covariates", {"history": hist.tolist(), "past_covariates": past,
                                                    "future_covariates": fut, "prediction_length": days})
    base = _chronos_forecast(hist.tolist(), days)
    nz = lambda v: round(max(0.0, v), 1)   # 销量不为负
    rows = []
    for i, d in enumerate(fut_idx):
        r = {"date": d.strftime("%Y-%m-%d"), "weekday": "一二三四五六日"[d.weekday()], "holiday": fut.get("holiday", [""] * days)[i],
             "promo": fut.get("promo_type", ["-"] * days)[i], "p10": nz(fc["0.1"][i]), "p50": nz(fc["0.5"][i]),
             "p90": nz(fc["0.9"][i]), "baseline_p50": nz(base["0.5"][i])}
        if "weather" in fut:
            r["weather"] = f"{fut['weather'][i]} {fut['temp_low'][i]:.0f}~{fut['temp_high'][i]:.0f}℃"
        rows.append(r)
    out = {"store": store, "sku": sku, "data_last_date": _last_date().strftime("%Y-%m-%d"),
           "covariates_used": sorted(fut.keys()), "promo_mode": promo_mode, "promo_type": promo_type, "promo_days": promo_days,
           "history_promo_share": round(promo_share, 2),
           "forecast": rows,
           "totals": {"with_factors_p50": round(sum(max(0.0, v) for v in fc["0.5"]), 1),
                      "with_factors_p90": round(sum(max(0.0, v) for v in fc["0.9"]), 1),
                      "baseline_p50": round(sum(max(0.0, v) for v in base["0.5"]), 1)}}
    if promo_share >= 0.8 and not promo_days:
        out["caution"] = (f"该商品历史 {promo_share:.0%} 的日子都在促销，没有『不促销』的历史样本，"
                          "不促销情景的预测属于外推，可信度低，建议只做方向性参考。")
    return out


@tool
def backtest(store: int, skus: list[int] | None = None, horizon: int = 7, folds: int = 4,
             history_days: int = 60, top_n: int = 10, compare_factors: bool = False) -> dict:
    """回测预测精度：在已有数据上滚动切分，用前面的历史预测后面 horizon 天，与真实销量比较，给出 WAPE（加权绝对百分比误差，越低越好）。
    用户问"预测准不准 / 准确率 / 误差多大"时调用。一次 batch_forecast 评估多个商品。

    Args:
        store: 门店代码
        skus: 商品代码列表；不传 = 该店近 30 天销量 Top-N
        horizon: 每折预测天数，默认 7
        folds: 滚动折数，默认 4（即回看最近 4 周）
        history_days: 每折使用的历史长度，默认 60
        top_n: skus 不传时取前几名，默认 10
        compare_factors: 是否同时回测"带业务因素"模型（节假日/促销/价格/天气用历史真实值），对比基线；多 N×折 次调用，约 10~30 秒
    """
    df = _Data.ensure()
    if not skus:
        skus = [k for s_, k, _ in batch.top_skus(df, [store], top_n, 30)]
    full = {k: _daily_series(store, k, len(_Data.days)) for k in skus}
    n = len(_Data.days)
    need = history_days + horizon * folds
    if n < need:
        folds = max(1, (n - history_days) // horizon)
    per_fold, per_sku_err = [], {k: [0.0, 0.0] for k in skus}
    fac_ae = fac_tot = 0.0
    for f in range(folds, 0, -1):
        cut = n - horizon * f                     # 用 [cut-history_days, cut) 预测 [cut, cut+horizon)
        series = [full[k].iloc[max(0, cut - history_days):cut].tolist() for k in skus]
        preds = _chronos_call("batch_forecast", {"time_series": series, "prediction_length": horizon})
        ae = tot = 0.0
        for k, p in zip(skus, preds):
            actual = full[k].iloc[cut:cut + horizon].tolist()
            e = sum(abs(a - b) for a, b in zip(p["0.5"], actual)); t = sum(actual)
            ae += e; tot += t; per_sku_err[k][0] += e; per_sku_err[k][1] += t
            if compare_factors:   # 用历史真实协变量（"事后已知"）回测带因素模型
                hidx = full[k].index[max(0, cut - history_days):cut]; fidx = full[k].index[cut:cut + horizon]
                pc = factors.past_covariates(df, store, k, hidx); fcov = factors.past_covariates(df, store, k, fidx)
                fcov = {key: fcov[key] for key in pc.keys() if key in fcov}
                pf = _chronos_call("forecast_with_covariates", {"history": series[skus.index(k)], "past_covariates": pc,
                                                                "future_covariates": fcov, "prediction_length": horizon})
                fac_ae += sum(abs(max(0.0, a) - b) for a, b in zip(pf["0.5"], actual)); fac_tot += t
        per_fold.append({"forecast_start": _Data.days[cut].strftime("%Y-%m-%d"), "wape": round(ae / tot, 4) if tot else None,
                         "actual_total": round(tot, 1)})
    overall_ae = sum(v[0] for v in per_sku_err.values()); overall_tot = sum(v[1] for v in per_sku_err.values())
    by_sku = sorted([{"sku": k, "wape": round(v[0] / v[1], 4) if v[1] else None, "actual_total": round(v[1], 1)}
                     for k, v in per_sku_err.items()], key=lambda x: (x["wape"] is None, x["wape"] or 0))
    return {"store": store, "skus": len(skus), "horizon": horizon, "folds": len(per_fold), "history_days": history_days,
            "overall_wape": round(overall_ae / overall_tot, 4) if overall_tot else None,
            "overall_accuracy_pct": round((1 - overall_ae / overall_tot) * 100, 1) if overall_tot else None,
            "per_fold": per_fold, "best_skus": by_sku[:3], "worst_skus": by_sku[-3:],
            **({"with_factors_wape": round(fac_ae / fac_tot, 4), "with_factors_accuracy_pct": round((1 - fac_ae / fac_tot) * 100, 1)}
               if compare_factors and fac_tot else {}),
            "note": "WAPE = Σ|预测-实际| / Σ实际；准确率 = 1 - WAPE。基线 = P50 点预测不带协变量；带因素 = 用历史真实的节假日/促销/价格/天气作协变量。"}


@tool
def inventory_status(store: int, sku: int) -> dict:
    """查商品主数据（品类、保质期、到货周期、箱规、起订量）和门店库存快照（现有库存、在途、最早到期日），
    并给出按近 14 天日均可售天数。用户问"库存多少 / 还能卖几天 / 保质期多长"时调用。

    Args:
        store: 门店代码
        sku: 商品代码
    """
    prod = MASTER.product(sku) if MASTER else None
    stock = MASTER.stock(store, sku) if MASTER else None
    hist = _daily_series(store, sku, 14)
    avg = float(hist.mean()) if len(hist) else 0.0
    out = {"store": store, "sku": sku, "product": prod or "无主数据", "stock": stock or "无库存快照",
           "avg_daily_last14": round(avg, 2)}
    if stock and avg > 0:
        out["days_of_cover"] = round((stock["on_hand"] + stock["in_transit"]) / avg, 1)
    if prod and prod.get("source") == "mock":
        out["note"] = "主数据与库存为演示用模拟数据（客户数据到位后替换）"
    return out


@tool
def replenishment_plan(store: int, sku: int, shelf_life_days: int | None = None, lead_time_days: int | None = None,
                       interval_days: int | None = None, case_pack: int | None = None, moq_cases: int | None = None,
                       on_hand: float | None = None, in_transit: float | None = None, earliest_expiry: str | None = None,
                       quantile: str | None = None, promo_days: list[str] | None = None) -> dict:
    """考虑保质期的补货计算：覆盖天数 = min(补货间隔+到货周期, 保质期−到货周期−1)；短保（≤7 天）按 P50、长保按 P90；
    扣减可用库存与在途（覆盖期内到期的库存不可用），按箱规向上取整、不低于起订量，并给出损耗风险。
    参数不传时自动取商品主数据与库存快照；用户口述的值优先。用户问"补多少 / 订几箱 / 要不要补货"时调用。

    Args:
        store: 门店代码
        sku: 商品代码
        shelf_life_days: 保质期（天）
        lead_time_days: 到货周期（天）
        interval_days: 补货间隔（天）
        case_pack: 箱规
        moq_cases: 起订量（箱）
        on_hand: 现有库存
        in_transit: 在途数量
        earliest_expiry: 现有库存最早到期日 YYYY-MM-DD
        quantile: 强制用哪个分位数备货："0.5" 或 "0.9"；不传按保质期自动
        promo_days: 覆盖期内的促销日期，有则用带因素预测
    """
    prod = (MASTER.product(sku) if MASTER else None) or {}
    stock = (MASTER.stock(store, sku) if MASTER else None) or {}
    params = {"shelf_life_days": shelf_life_days or prod.get("shelf_life_days"), "lead_time_days": lead_time_days if lead_time_days is not None else prod.get("lead_time_days"),
              "interval_days": interval_days or prod.get("replenish_interval_days"), "case_pack": case_pack or prod.get("case_pack"),
              "moq_cases": moq_cases or prod.get("moq_cases", 1), "on_hand": on_hand if on_hand is not None else stock.get("on_hand"),
              "in_transit": in_transit if in_transit is not None else stock.get("in_transit", 0),
              "earliest_expiry": earliest_expiry or stock.get("earliest_expiry")}
    missing = [k for k in ("shelf_life_days", "lead_time_days", "interval_days", "case_pack", "on_hand") if params[k] is None]
    if missing:
        return {"error": f"缺少参数且主数据/库存快照里也没有：{missing}，请用户提供", "known": {k: v for k, v in params.items() if v is not None}}
    horizon = int(params["lead_time_days"]) + max(1, min(int(params["interval_days"]) + int(params["lead_time_days"]),
                                                       int(params["shelf_life_days"]) - int(params["lead_time_days"]) - 1))
    horizon = max(horizon, 1)
    if promo_days:
        fc_full = forecast_sku_with_factors(store, sku, days=horizon, promo_days=promo_days)
        fc = {"0.1": [r["p10"] for r in fc_full["forecast"]], "0.5": [r["p50"] for r in fc_full["forecast"]],
              "0.9": [r["p90"] for r in fc_full["forecast"]]}
    else:
        hist = _daily_series(store, sku, 60)
        if hist.sum() == 0:
            return {"error": f"门店 {store} 商品 {sku} 最近 60 天没有销售记录"}
        fc = _chronos_forecast(hist.tolist(), horizon)
    result = master.plan(fc, _last_date(), int(params["shelf_life_days"]), int(params["lead_time_days"]), int(params["interval_days"]),
                         int(params["case_pack"]), int(params["moq_cases"]), float(params["on_hand"]), float(params["in_transit"]),
                         params["earliest_expiry"], quantile)
    result.update({"store": store, "sku": sku, "category": prod.get("category"), "params_used": params,
                   "data_last_date": _last_date().strftime("%Y-%m-%d"),
                   "source_note": "主数据/库存为演示用模拟数据" if prod.get("source") == "mock" or stock.get("source") == "mock" else "主数据/库存来自客户"})
    return result


@tool
def forecast_top_skus(stores: list[int] | None = None, n: int = 10, days: int = 7, history_days: int = 60) -> dict:
    """批量预测：各门店近 30 天销量 Top-N 商品的未来 days 天销量，一次调用完成，生成 Excel 报表存到 S3。
    用户要"所有门店 / 多个门店的热销商品批量预测、补货清单、报表"时调用；单个商品用 forecast_sku。
    返回每店合计与前 3 名摘要以及报表位置，不要逐行复述报表。

    Args:
        stores: 门店代码列表，不传 = 全部门店
        n: 每店取前几名，默认 10
        days: 预测天数，默认 7
        history_days: 历史窗口天数，默认 60
    """
    if not CHART_BUCKET:
        return {"error": "未配置 CHART_BUCKET，无法保存报表"}
    df = _Data.ensure()
    t0 = time.time()

    def _bf(series, pdays):
        return _chronos_call("batch_forecast", {"time_series": series, "prediction_length": pdays})

    report = batch.run_batch(df, _bf, stores, n, days, history_days)
    meta = {"生成时间": time.strftime("%Y-%m-%d %H:%M"), "数据截止": _last_date().strftime("%Y-%m-%d"),
            "门店": "全部" if not stores else ",".join(map(str, stores)), "每店 Top": n, "预测天数": days,
            "历史窗口": history_days, "模型": "Chronos-2（Amazon Bedrock AgentCore Runtime, CPU）",
            "口径": "P50 = 常规备货量；P90 = 安全库存上限"}
    key = f"{REPORT_PREFIX}top{n}_{days}d_{time.strftime('%Y%m%d-%H%M%S')}.xlsx"
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=CHART_BUCKET, Key=key, Body=batch.to_excel_bytes(report, meta),
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    logger.info(f"batch report rows={len(report)} s3://{CHART_BUCKET}/{key} latency_ms={(time.time() - t0) * 1000:.0f}")
    summary = batch.summarize(report, days)
    summary.update({"report_s3_uri": f"s3://{CHART_BUCKET}/{key}", "data_last_date": meta["数据截止"],
                    "note": "完整逐日 P50/P90 在 Excel 报表里，前端可直接下载"})
    return summary


@tool
def list_top_skus(store: int, n: int = 10, days: int = 30) -> dict:
    """列出某门店最近 days 天销量最高的 n 个商品。

    Args:
        store: 门店代码（1~6）
        n: 返回数量
        days: 统计最近多少天
    """
    SALES = _Data.ensure()
    d = SALES[(SALES.store == store) & (SALES.date > _last_date() - pd.Timedelta(days=days))]
    top = d.groupby("sku")["qty"].sum().sort_values(ascending=False).head(n)
    return {"store": store, "days": days, "data_last_date": _last_date().strftime("%Y-%m-%d"),
            "top_skus": [{"sku": int(k), "total_qty": round(float(v), 1)} for k, v in top.items()]}


@tool
def recent_sales(store: int, sku: int, days: int = 14) -> dict:
    """查询某门店某商品最近 days 天的每日实际销量和天气。

    Args:
        store: 门店代码（1~6）
        sku: 商品代码
        days: 最近多少天
    """
    s = _daily_series(store, sku, days)
    SALES = _Data.ensure()
    wx = (SALES[SALES.store == store].drop_duplicates("date").set_index("date")
          .reindex(s.index)[["weather", "temp_low", "temp_high"]])
    return {"store": store, "sku": sku, "daily": [
        {"date": d.strftime("%Y-%m-%d"), "qty": round(float(q), 1), "weather": str(wx.loc[d, "weather"]),
         "temp": f"{wx.loc[d, 'temp_low']}~{wx.loc[d, 'temp_high']}℃"} for d, q in s.items()]}


# --- AgentCore Code Interpreter：隔离沙箱里做精确计算和画图（中国区可用） ---

_ci: CodeInterpreter | None = None
_ci_lock = threading.Lock()


def _code_interpreter() -> CodeInterpreter:
    """每个 Agent 会话（microVM）复用一个 Code Interpreter 会话，首次调用时启动（约 2 s）。"""
    global _ci
    with _ci_lock:
        if _ci is None:
            t0 = time.time()
            ci = CodeInterpreter(region=REGION)
            ci.start(session_timeout_seconds=900)
            logger.info(f"code interpreter session {ci.session_id} started in {time.time() - t0:.2f}s")
            _ci = ci
    return _ci


def _ci_result(resp) -> dict:
    return list(resp["stream"])[0]["result"] if "stream" in resp else resp


def _decode_blob(raw) -> bytes:
    """readFiles 返回的二进制内容：SDK 1.24 把 bytes 转成了 repr 字符串（"b'\\x89PNG...'"），兼容三种形态。"""
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str) and raw[:2] in ("b'", 'b"'):
        return ast.literal_eval(raw)
    return base64.b64decode(raw)


def _ci_exec(code: str) -> dict:
    t0 = time.time()
    res = _ci_result(_code_interpreter().invoke("executeCode", {"code": code, "language": "python"}))
    sc = res.get("structuredContent") or {}
    logger.info(f"code interpreter exec exit={sc.get('exitCode')} latency_ms={(time.time() - t0) * 1000:.0f}")
    return {"stdout": sc.get("stdout", ""), "stderr": sc.get("stderr", ""), "exit_code": sc.get("exitCode")}


@tool
def run_python(code: str) -> dict:
    """在隔离沙箱中执行 Python 代码做精确计算，用 print 输出结果。
    适用：补货量 = P90 合计 − 现有库存 − 在途，按箱规向上取整；多商品汇总、占比、同比环比等。
    不要用它查询销量数据，查询用 recent_sales / list_top_skus。

    Args:
        code: 要执行的 Python 代码，必须用 print 输出最终结果
    """
    return _ci_exec(code)


@tool
def plot_forecast(store: int, sku: int, days: int = 7, history_days: int = 30) -> dict:
    """绘制某门店某商品的历史日销量与未来预测区间图（PNG），保存到 S3 并返回位置。
    用户要求"画图 / 图表 / 可视化 / 趋势图"时调用。

    Args:
        store: 门店代码（1~6）
        sku: 商品代码
        days: 预测天数，默认 7
        history_days: 图中显示最近多少天历史，默认 30
    """
    if not CHART_BUCKET:
        return {"error": "未配置 CHART_BUCKET，无法保存图表"}
    hist = _daily_series(store, sku, history_days)
    if hist.sum() == 0:
        return {"error": f"门店 {store} 商品 {sku} 最近 {history_days} 天没有销售记录"}
    fc = _chronos_forecast(_daily_series(store, sku, 60).tolist(), days)
    fut = pd.date_range(_last_date() + pd.Timedelta(days=1), periods=days, freq="D")
    payload = {"hist_dates": [d.strftime("%m-%d") for d in hist.index], "hist": [float(x) for x in hist],
               "fut_dates": [d.strftime("%m-%d") for d in fut],
               "p10": fc["0.1"], "p50": fc["0.5"], "p90": fc["0.9"], "title": f"Store {store} / SKU {sku}"}
    fname = f"forecast_{store}_{sku}.png"
    # 沙箱里没有中文字体，图内文字用英文；数据通过 JSON 字面量传入
    code = f"""
import json, matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
d = json.loads({json.dumps(json.dumps(payload, ensure_ascii=False))})
x_h = list(range(len(d['hist']))); x_f = list(range(len(d['hist']), len(d['hist']) + len(d['p50'])))
plt.figure(figsize=(10, 4))
plt.plot(x_h, d['hist'], color='#232F3E', label='Actual')
plt.plot(x_f, d['p50'], color='#FF9900', marker='o', label='Forecast P50')
plt.fill_between(x_f, d['p10'], d['p90'], color='#FF9900', alpha=0.2, label='P10-P90')
ticks = x_h[::5] + x_f; labels = d['hist_dates'][::5] + d['fut_dates']
plt.xticks(ticks, labels, rotation=45, fontsize=8)
plt.title(d['title'] + ' daily sales forecast'); plt.ylabel('units'); plt.legend(); plt.grid(alpha=0.3); plt.tight_layout()
plt.savefig('{fname}', dpi=100); print('ok')
"""
    out = _ci_exec(code)
    if out["exit_code"] != 0:
        return {"error": f"绘图失败: {out['stderr'][-300:]}"}
    res = _ci_result(_code_interpreter().invoke("readFiles", {"paths": [fname]}))
    png = _decode_blob(res["content"][0]["resource"]["blob"])
    key = f"{CHART_PREFIX}{store}_{sku}_{uuid.uuid4().hex[:8]}.png"
    boto3.client("s3", region_name=REGION).put_object(Bucket=CHART_BUCKET, Key=key, Body=png, ContentType="image/png")
    logger.info(f"chart saved s3://{CHART_BUCKET}/{key} bytes={len(png)}")
    return {"chart_s3_uri": f"s3://{CHART_BUCKET}/{key}", "size_bytes": len(png),
            "forecast_total_p50": round(sum(fc["0.5"]), 1), "forecast_total_p90": round(sum(fc["0.9"]), 1),
            "note": "图表已保存到私有 S3，调用方用 IAM 凭证下载后展示给用户"}


SYSTEM_PROMPT = f"""你是便利店连锁的补货助手，服务门店运营人员。
你可以调用工具查询门店-商品的历史销量，并用 Chronos-2 模型预测未来销量。
数据范围：门店 1~6，日销量数据截止到 {{last_date}}，"下周"/"未来7天"指数据截止日之后的 7 天。
回答要求：
- 不要自己猜销量，一律调用 forecast_sku 获取预测；用户关心天气影响时用 forecast_sku_with_weather（会同时给出不带天气的基线）。
- 门店位于{DEFAULT_CITY}。查天气用 daily_weather（Open-Meteo），不要编造天气。
- 用户问补多少、订几箱时用 replenishment_plan（自动带入商品主数据与库存快照，考虑保质期；用户口述的库存/箱规优先），并说明覆盖天数和用的分位数；
  只有 replenishment_plan 缺参数且用户已给全部数字时才用 run_python 手算。问库存、保质期用 inventory_status。
- 其他多商品汇总、占比等计算用 run_python，不要心算。
- 用户要图表时调用 plot_forecast，并把返回的 S3 位置告诉用户。
- 用户提到促销、活动、节假日（五一、国庆等）时用 forecast_sku_with_factors，促销日期用 promo_days 传入；比较"促销 vs 不促销"就调用两次。
- 用户问准不准、准确率、误差时用 backtest，汇报整体 WAPE / 准确率和最好最差的商品，说明这是历史回测结果。
- 用户要多门店 / 热销商品的批量预测或补货清单时，调用 forecast_top_skus（一次完成并生成 Excel），只汇报每店合计和前几名，并给出报表的 S3 位置。
- 用中文、简洁地给出：预测的每日 P50、合计 P50（常规备货量）和 P90（安全库存上限），并简述近期销量趋势。
- 用户只说商品名没有商品代码时，用 list_top_skus 帮用户定位，或请用户提供商品代码。
"""


# --- LLM：OpenAI 兼容接口（PPIO DeepSeek）。API Key 来自 AgentCore Identity，本地调试用环境变量 ---

def _get_api_key() -> str:
    if os.environ.get("PPIO_API_KEY"):
        return os.environ["PPIO_API_KEY"]
    from bedrock_agentcore.identity.auth import requires_api_key

    @requires_api_key(provider_name=API_KEY_PROVIDER)
    def _fetch(*, api_key: str) -> str:
        return api_key

    t0 = time.time()
    key = _fetch()
    logger.info(f"api key fetched from AgentCore Identity provider={API_KEY_PROVIDER} in {time.time() - t0:.2f}s")
    return key


_agent: Agent | None = None


def _get_agent() -> Agent:
    """每个会话一个 microVM，所以进程内单个 Agent 即可保存本会话的对话历史。"""
    global _agent
    if _agent is None:
        chronos.start()
        weather.start()
        model = OpenAIModel(client_args={"api_key": _get_api_key(), "base_url": LLM_BASE_URL},
                            model_id=LLM_MODEL, params={"max_tokens": 2000, "temperature": 0.2})
        # 天气 MCP 的 daily_weather / geocode 直接暴露给 LLM，便于回答"下周西安天气怎样"类问题
        try:
            last_date = _last_date().strftime("%Y-%m-%d")
        except Exception as e:  # noqa: BLE001
            last_date = f"未知（销售数据尚未上传到 {DATA_URI}：{e}）"
        _agent = Agent(model=model,
                       tools=[forecast_sku, forecast_sku_with_weather, forecast_sku_with_factors, forecast_top_skus,
                              backtest, replenishment_plan, inventory_status, list_top_skus, recent_sales,
                              run_python, plot_forecast] + weather.list_tools_sync(),
                       system_prompt=SYSTEM_PROMPT.replace("{last_date}", last_date), callback_handler=None)
    return _agent


@app.entrypoint
def invoke(payload: dict, context=None) -> dict:
    prompt = payload.get("prompt", "")
    t0 = time.time()
    agent = _get_agent()
    result = agent(prompt)
    m = result.metrics
    tools_used = [{"tool": name, "calls": tm.call_count, "ms": round(tm.total_time * 1000)}
                  for name, tm in m.tool_metrics.items()]
    resp = {"result": str(result).strip(), "usage": dict(m.accumulated_usage),
            "model": LLM_MODEL, "model_endpoint": urllib.parse.urlparse(LLM_BASE_URL).netloc,
            "llm_ms": round(sum(m.accumulated_metrics.values()) if isinstance(m.accumulated_metrics, dict)
                            else m.accumulated_metrics.get("latencyMs", 0)),
            "tools": tools_used, "total_ms": round((time.time() - t0) * 1000),
            "session_id": getattr(context, "session_id", None)}
    logger.info(f"invoke done total_ms={resp['total_ms']} usage={resp['usage']} tools={tools_used}")
    return resp


if __name__ == "__main__":
    app.run()
