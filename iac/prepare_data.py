"""把门店销售 Excel 转成 Agent 读取的 parquet（列名固定，Agent 代码依赖这些列）。

用法：python deploy/prepare_data.py <销售数据.xlsx> data/sales_daily.parquet

输入 Excel：每个 sheet 一家门店，列：记账日期、门店代码、门店类型、商品代码、销售数量、天气、温度（如 "3℃~6℃"）、星期
输出 parquet 列：date, store, store_type, sku, qty, weather, temp_low, temp_high, weekday
生产环境可以用同样的列结构从 ERP/数据库导出，每日覆盖上传到 S3 即可。
"""
import os
import re
import sys

import pandas as pd

src, dst = sys.argv[1], sys.argv[2]
df = pd.concat(pd.read_excel(src, sheet_name=None).values(), ignore_index=True)


def temps(s):
    n = [int(x) for x in re.findall(r"-?\d+", str(s))]
    return (min(n), max(n)) if n else (None, None)


df["temp_low"], df["temp_high"] = zip(*df["温度"].map(temps))
out = pd.DataFrame({
    "date": pd.to_datetime(df["记账日期"]), "store": df["门店代码"].astype(int),
    "store_type": df["门店类型"].astype(str), "sku": df["商品代码"].astype(int),
    "qty": df["销售数量"].astype(float), "weather": df["天气"].astype(str),
    "temp_low": df["temp_low"], "temp_high": df["temp_high"], "weekday": df["星期"].astype(str),
})
os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
out.to_parquet(dst, index=False)
print(f"{len(out)} rows, {out.date.min().date()} ~ {out.date.max().date()}, "
      f"{out.store.nunique()} stores, {out.sku.nunique()} skus -> {dst} ({os.path.getsize(dst) // 1024} KB)")
