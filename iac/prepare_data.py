"""把门店销售 Excel 转成 Agent 读取的 parquet（列名固定，Agent 代码依赖这些列）。

用法：python iac/prepare_data.py <销售数据.xlsx> data/sales_daily.parquet

输入 Excel：每个 sheet 一家门店，列：记账日期、门店代码、门店类型、商品代码、销售数量、天气、温度（如 "3℃~6℃"）、星期
输出 parquet 列：date, store, store_type, sku, qty, weather, temp_low, temp_high, weekday
前端的"上传 Excel"也调用这里的 convert_excel()，保证两条路径口径一致。
"""
import io
import os
import re
import sys

import pandas as pd

REQUIRED = ["记账日期", "门店代码", "商品代码", "销售数量"]
OPTIONAL = ["门店类型", "天气", "温度", "星期"]


def _temps(s):
    n = [int(x) for x in re.findall(r"-?\d+", str(s))]
    return (min(n), max(n)) if n else (None, None)


def convert_excel(src) -> pd.DataFrame:
    """src 可以是路径或文件对象。缺少必需列、日期无法解析时抛 ValueError（中文提示）。"""
    sheets = pd.read_excel(src, sheet_name=None)
    df = pd.concat(sheets.values(), ignore_index=True)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"Excel 缺少必需列：{missing}；需要 {REQUIRED}，可选 {OPTIONAL}")
    for c in OPTIONAL:
        if c not in df.columns:
            df[c] = None
    dates = pd.to_datetime(df["记账日期"], errors="coerce")
    if dates.isna().mean() > 0.01:
        raise ValueError("“记账日期”列有超过 1% 无法解析为日期，请检查格式（如 2026-05-24）")
    df = df[dates.notna()].copy()
    df["temp_low"], df["temp_high"] = zip(*df["温度"].map(_temps))
    out = pd.DataFrame({
        "date": pd.to_datetime(df["记账日期"]).dt.normalize(),
        "store": pd.to_numeric(df["门店代码"], errors="coerce"),
        "store_type": df["门店类型"].astype(str),
        "sku": pd.to_numeric(df["商品代码"], errors="coerce"),
        "qty": pd.to_numeric(df["销售数量"], errors="coerce"),
        "weather": df["天气"].astype(str),
        "temp_low": df["temp_low"], "temp_high": df["temp_high"],
        "weekday": df["星期"].astype(str),
    }).dropna(subset=["store", "sku", "qty"])
    out["store"] = out["store"].astype(int)
    out["sku"] = out["sku"].astype(int)
    out["qty"] = out["qty"].astype(float)
    if out.empty:
        raise ValueError("转换后没有有效数据行（门店代码 / 商品代码 / 销售数量 需为数字）")
    return out


def to_parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    return buf.getvalue()


def describe(df: pd.DataFrame) -> str:
    return (f"{len(df)} 行，{df.date.min().date()} ~ {df.date.max().date()}，"
            f"{df.store.nunique()} 家门店，{df.sku.nunique()} 个商品")


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    out = convert_excel(src)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    out.to_parquet(dst, index=False)
    print(f"{describe(out)} -> {dst} ({os.path.getsize(dst) // 1024} KB)")
