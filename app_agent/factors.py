"""业务因素（协变量）构造：节假日、促销、价格、天气、星期，供带因素预测和回测使用。

- 历史侧：直接取销售数据里的列（节假日 / 促销类型 / 促销数量 / 优惠金额 / 门店售价 / 天气 / 温度）。
- 未来侧：节假日用 chinesecalendar 离线日历（2004~当前已公布年份）；促销由用户给出的计划生成；
  天气由 Open-Meteo（调用方注入）；价格默认延续最近一次售价。
与 8 月 SageMaker 版验证过的 13 个协变量口径保持一致。
"""
import datetime as dt

import pandas as pd

try:
    import chinese_calendar as cc
except Exception:  # noqa: BLE001  未安装时退化为"周末/工作日"
    cc = None

HOLIDAY_CN = {"New Year's Day": "元旦", "Spring Festival": "春节", "Tomb-sweeping Day": "清明节",
              "Labour Day": "劳动节", "Dragon Boat Festival": "端午节", "Mid-autumn Festival": "中秋节",
              "National Day": "国庆节", "Anti-Fascist 70th Day": "抗战胜利纪念日"}
HOLIDAY_RANK = {"工作日": 0, "周末调班": 0, "周末": 1}   # 其余节日 = 2，供 holiday_num 数值协变量


def holiday_label(d: dt.date) -> str:
    """与客户数据"节假日"列同口径：工作日 / 周末 / 周末调班 / <节日名>。"""
    if cc is not None:
        try:
            on_holiday, name = cc.get_holiday_detail(d)
            if on_holiday and name:
                return HOLIDAY_CN.get(name, name)
            if on_holiday:
                return "周末"
            return "周末调班" if d.weekday() >= 5 else "工作日"
        except Exception:  # noqa: BLE001  年份超出日历数据范围
            pass
    return "周末" if d.weekday() >= 5 else "工作日"


def holiday_num(label: str) -> float:
    return float(HOLIDAY_RANK.get(label, 2))


def past_covariates(df: pd.DataFrame, store: int, sku: int, index: pd.DatetimeIndex) -> dict:
    """从销售数据提取历史协变量，按日期对齐；缺失特征用中性值填充。"""
    rows = df[(df.store == store) & (df.sku == sku)].drop_duplicates("date").set_index("date").reindex(index)
    store_day = df[df.store == store].drop_duplicates("date").set_index("date").reindex(index)
    cov = {"weekday_num": [float(d.weekday() + 1) for d in index]}
    hol = (store_day["holiday"] if "holiday" in store_day else pd.Series(index=index, dtype="string")).astype("string")
    hol = hol.where(hol.notna(), [holiday_label(d.date()) for d in index])
    cov["holiday"] = hol.astype(str).tolist()
    cov["holiday_num"] = [holiday_num(h) for h in cov["holiday"]]
    if "weather" in store_day:
        cov["weather"] = store_day["weather"].astype("string").ffill().bfill().fillna("多云").astype(str).tolist()
        cov["temp_low"] = store_day["temp_low"].astype(float).ffill().bfill().fillna(15).tolist()
        cov["temp_high"] = store_day["temp_high"].astype(float).ffill().bfill().fillna(25).tolist()
    if "promo_type" in rows:
        pt = rows["promo_type"].astype("string").fillna("无促销").astype(str)
        cov["promo_type"] = pt.tolist()
        cov["has_promotion"] = [0.0 if v == "无促销" else 1.0 for v in pt]
        cov["promo_qty"] = rows["promo_qty"].astype(float).fillna(0.0).tolist()
        cov["promo_discount"] = rows["promo_discount"].astype(float).fillna(0.0).tolist()
    if "price" in rows:
        cov["price"] = rows["price"].astype(float).ffill().bfill().fillna(0.0).tolist()
    return cov


def future_covariates(past: dict, index: pd.DatetimeIndex, promo_days: list[str] | None = None,
                      promo_type: str = "促销价", discount_per_unit: float | None = None,
                      weather_days: list[dict] | None = None) -> dict:
    """根据历史协变量的键构造未来协变量：节假日用日历，促销按用户计划，天气按 Open-Meteo，价格延续最近值。"""
    fut = {"weekday_num": [float(d.weekday() + 1) for d in index]}
    labels = [holiday_label(d.date()) for d in index]
    if "holiday" in past:
        fut["holiday"] = labels
        fut["holiday_num"] = [holiday_num(h) for h in labels]
    if "weather" in past:
        if weather_days and len(weather_days) >= len(index):
            fut["weather"] = [w["weather"] or "多云" for w in weather_days[:len(index)]]
            fut["temp_low"] = [float(w["temp_low"]) for w in weather_days[:len(index)]]
            fut["temp_high"] = [float(w["temp_high"]) for w in weather_days[:len(index)]]
        else:  # 没有天气预报时延续最近 7 天的平均
            fut["weather"] = [past["weather"][-1]] * len(index)
            fut["temp_low"] = [sum(past["temp_low"][-7:]) / 7] * len(index)
            fut["temp_high"] = [sum(past["temp_high"][-7:]) / 7] * len(index)
    if "promo_type" in past:
        promo_set = set(promo_days or [])
        avg_disc = discount_per_unit
        if avg_disc is None:
            hist = [d / q for d, q in zip(past["promo_discount"], past["promo_qty"]) if q > 0]
            avg_disc = round(sum(hist) / len(hist), 2) if hist else 1.0
        avg_qty = [q for q in past["promo_qty"] if q > 0]
        avg_qty = round(sum(avg_qty) / len(avg_qty), 1) if avg_qty else 1.0
        fut["promo_type"] = [promo_type if d.strftime("%Y-%m-%d") in promo_set else "无促销" for d in index]
        fut["has_promotion"] = [1.0 if v != "无促销" else 0.0 for v in fut["promo_type"]]
        fut["promo_qty"] = [avg_qty if h else 0.0 for h in fut["has_promotion"]]
        fut["promo_discount"] = [round(avg_disc * avg_qty, 2) if h else 0.0 for h in fut["has_promotion"]]
    if "price" in past:
        fut["price"] = [past["price"][-1]] * len(index)
    return fut


def wape(pred: list[float], actual: list[float]) -> float:
    denom = sum(abs(a) for a in actual)
    return round(sum(abs(p - a) for p, a in zip(pred, actual)) / denom, 4) if denom else float("nan")
