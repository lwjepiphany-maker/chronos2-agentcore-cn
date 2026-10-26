"""从客户真实门店数据构造测试用例（每组都留出最后 7 天作为真实值，用于对比）。

用法：python tests/make_payloads.py <门店销售数据.xlsx>
"""
import json
import re
import sys
from pathlib import Path

import pandas as pd

XLSX = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "每一天便利店测试门店商品数据0527.xlsx"
OUT = Path(__file__).parent / "payloads"
OUT.mkdir(exist_ok=True)
H = 7  # 预测天数

sheets = pd.read_excel(XLSX, sheet_name=None)
df = pd.concat(sheets.values(), ignore_index=True)
days = pd.date_range(df["记账日期"].min(), df["记账日期"].max(), freq="D")
print(f"rows={len(df)} days={len(days)} ({days[0].date()} ~ {days[-1].date()}) "
      f"stores={df['门店代码'].nunique()} skus={df['商品代码'].nunique()}")

# 每个门店-商品的日销量（缺失日补 0）
pivot = (df.groupby(["门店代码", "商品代码", "记账日期"])["销售数量"].sum()
           .unstack("记账日期").reindex(columns=days, fill_value=0).fillna(0))
pivot = pivot[pivot.sum(axis=1) > 0]
top = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).index]

# 每日天气/温度/星期（门店级，取 5 号店）
WEEK = {"星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4, "星期五": 5, "星期六": 6, "星期日": 7}
daily = (df[df["门店代码"] == 5].drop_duplicates("记账日期").set_index("记账日期")
           .reindex(days).ffill())

def temps(s):
    nums = [int(n) for n in re.findall(r"-?\d+", str(s))]
    return (min(nums), max(nums)) if nums else (0, 0)

daily["temp_low"], daily["temp_high"] = zip(*daily["温度"].map(temps))
daily["weekday_num"] = daily["星期"].map(WEEK)

def series(row, n_hist):
    v = row.values.astype(float).tolist()
    return v[-(n_hist + H):-H], v[-H:]

def save(name, payload, actual, meta):
    (OUT / f"{name}.json").write_text(json.dumps(
        {"meta": meta, "tool": payload["tool"], "arguments": payload["arguments"], "actual": actual},
        ensure_ascii=False))
    print(f"  {name}: {meta}")

# 1) 单品：5 号店销量最高的商品，30 天历史（与原方案"5号店30天销量"的端到端测试一致）
s5 = top.loc[5]
sku = s5.index[0]
hist, act = series(s5.loc[sku], 30)
save("single_30d", {"tool": "forecast", "arguments": {"history": hist, "prediction_length": H}},
     act, {"store": 5, "sku": int(sku), "history_days": 30})

# 2) 单品 + 协变量（天气-类别型、温度-数值型、星期-数值型）
n = 60
hist, act = series(s5.loc[sku], n)
idx_hist, idx_fut = slice(-(n + H), -H), slice(-H, None)
def cov(idx):
    d = daily.iloc[idx]
    return {"weather": d["天气"].astype(str).tolist(),
            "temp_low": d["temp_low"].astype(float).tolist(),
            "temp_high": d["temp_high"].astype(float).tolist(),
            "weekday_num": d["weekday_num"].astype(float).tolist()}
save("covariates_60d", {"tool": "forecast_with_covariates", "arguments": {
        "history": hist, "past_covariates": cov(idx_hist), "future_covariates": cov(idx_fut),
        "prediction_length": H}},
     act, {"store": 5, "sku": int(sku), "history_days": n, "covariates": list(cov(idx_fut))})

# 3) 批量：全部门店销量 Top100 的门店-商品组合，60 天历史
# 4) 批量：Top1000，用于估算每日跑批
for k in (100, 1000):
    rows = top.head(k)
    hs, acts = zip(*(series(r, 60) for _, r in rows.iterrows()))
    save(f"batch_{k}", {"tool": "batch_forecast", "arguments": {"time_series": list(hs), "prediction_length": H}},
         list(acts), {"n_series": k, "history_days": 60})
