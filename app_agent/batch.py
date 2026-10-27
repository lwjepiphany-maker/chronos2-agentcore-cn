"""批量预测的共享核心逻辑：Agent 工具 forecast_top_skus 和命令行 tools/batch_forecast.py 都用它。

流程：日销量 DataFrame → 每店 Top-N 商品 → 一次 Chronos batch_forecast → 报表 DataFrame → Excel 字节。
Chronos 调用由调用方注入（Agent 里是 MCPClient，脚本里是 SigV4 MCP 客户端），这里只负责数据整形。
"""
import io
from typing import Callable

import pandas as pd

QUANTILES = ("0.1", "0.5", "0.9")


def latest_window(dates: pd.Series) -> pd.DatetimeIndex:
    """客户数据分段（如 2025 春季 + 2026 春季），只取最后一段连续日期。"""
    d = pd.Series(sorted(pd.to_datetime(dates).unique()))
    breaks = d[d.diff() != pd.Timedelta(days=1)].index
    return pd.DatetimeIndex(d.iloc[breaks[-1]:].values)


def top_skus(df: pd.DataFrame, stores: list[int] | None, n: int, days: int) -> list[tuple[int, int, float]]:
    """返回 [(store, sku, 近 days 天销量)]，每店按销量取前 n。"""
    last = df.date.max()
    recent = df[df.date > last - pd.Timedelta(days=days)]
    if stores:
        recent = recent[recent.store.isin(stores)]
    g = recent.groupby(["store", "sku"])["qty"].sum().reset_index()
    out = []
    for store, grp in g.groupby("store"):
        for _, r in grp.sort_values("qty", ascending=False).head(n).iterrows():
            out.append((int(store), int(r.sku), float(r.qty)))
    return out


def series_for(df: pd.DataFrame, days_index: pd.DatetimeIndex, store: int, sku: int, history_days: int) -> list[float]:
    s = (df[(df.store == store) & (df.sku == sku)].groupby("date")["qty"].sum()
         .reindex(days_index, fill_value=0.0))
    return [float(x) for x in s.iloc[-history_days:]]


def run_batch(df: pd.DataFrame, batch_forecast: Callable[[list[list[float]], int], list[dict]],
              stores: list[int] | None = None, n: int = 10, days: int = 7, history_days: int = 60,
              rank_days: int = 30, chunk: int = 200) -> pd.DataFrame:
    """每店 Top-N → 一次（或分块）batch_forecast → 逐日 + 汇总的报表 DataFrame。"""
    days_index = latest_window(df.date)
    picks = top_skus(df, stores, n, rank_days)
    histories = [series_for(df, days_index, s, k, history_days) for s, k, _ in picks]
    preds: list[dict] = []
    for i in range(0, len(histories), chunk):
        preds += batch_forecast(histories[i:i + chunk], days)
    fut = pd.date_range(days_index[-1] + pd.Timedelta(days=1), periods=days, freq="D")
    rows = []
    for rank_i, ((store, sku, recent_qty), p) in enumerate(zip(picks, preds)):
        row = {"门店": store, "商品代码": sku, f"近{rank_days}天销量": round(recent_qty, 1),
               "历史日均": round(sum(histories[rank_i]) / len(histories[rank_i]), 2),
               f"未来{days}天P50合计(常规备货)": round(sum(p["0.5"]), 1),
               f"未来{days}天P90合计(安全上限)": round(sum(p["0.9"]), 1)}
        for i, d in enumerate(fut):
            row[f"{d.strftime('%m-%d')} P50"] = round(p["0.5"][i], 1)
        for i, d in enumerate(fut):
            row[f"{d.strftime('%m-%d')} P90"] = round(p["0.9"][i], 1)
        rows.append(row)
    report = pd.DataFrame(rows)
    report.insert(2, "店内排名", report.groupby("门店").cumcount() + 1)
    return report


def summarize(report: pd.DataFrame, days: int) -> dict:
    """给 LLM 的精简摘要：每店合计 + 每店前 3 名，不把整张表塞进上下文。"""
    p50 = [c for c in report.columns if c.endswith("P50合计(常规备货)")][0]
    p90 = [c for c in report.columns if c.endswith("P90合计(安全上限)")][0]
    per_store = []
    for store, g in report.groupby("门店"):
        top3 = [{"sku": int(r["商品代码"]), "p50_total": float(r[p50]), "p90_total": float(r[p90])}
                for _, r in g.sort_values(p50, ascending=False).head(3).iterrows()]
        per_store.append({"store": int(store), "skus": len(g), "p50_total": round(float(g[p50].sum()), 1),
                          "p90_total": round(float(g[p90].sum()), 1), "top3": top3})
    return {"days": days, "stores": per_store, "rows": len(report)}


def to_excel_bytes(report: pd.DataFrame, meta: dict) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        report.to_excel(xw, index=False, sheet_name="预测结果")
        pd.DataFrame([meta]).T.reset_index().rename(columns={"index": "项目", 0: "值"}).to_excel(
            xw, index=False, sheet_name="说明")
        ws = xw.sheets["预测结果"]
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = max(10, min(28, max(len(str(c.value or "")) for c in col) + 2))
    return buf.getvalue()
