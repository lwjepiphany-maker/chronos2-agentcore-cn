"""商品主数据与库存快照的读取，以及考虑保质期的补货量计算。

补货决策 ≠ 销量预测：预测给出逐日 P10/P50/P90，这里在其上叠加保质期、到货周期、补货间隔、库存与在途、箱规与起订量。
公式：
  覆盖天数 D = min(补货间隔 + 到货周期, 保质期 − 到货周期 − 1)，至少 1 天
  需求      = 第 L+1 ~ L+D 天预测分位数之和（短保用 P50，长保用 P90，可指定）
  可用库存  = 现有库存（若最早到期日落在覆盖期内则视为不可用）+ 在途
  补货量    = max(0, 需求 − 可用库存)，按箱规向上取整，不低于起订量
"""
import io
import math
import time

import boto3
import pandas as pd

PERISHABLE_DAYS = 7   # 保质期 ≤ 7 天视为短保


class Master:
    """products / inventory 两张表的延迟加载与定期刷新（60 s 内复用）。缺表时返回 None，由工具按用户参数兜底。"""

    def __init__(self, bucket: str, prefix: str, region: str):
        self.bucket, self.prefix, self.region = bucket, prefix.rstrip("/") + "/", region
        self.products = self.inventory = None
        self.loaded_at = 0.0

    def _read(self, name):
        s3 = boto3.client("s3", region_name=self.region)
        body = s3.get_object(Bucket=self.bucket, Key=self.prefix + name)["Body"].read()
        return pd.read_parquet(io.BytesIO(body))

    def ensure(self):
        if time.time() - self.loaded_at < 60 and self.products is not None:
            return
        try:
            self.products = self._read("products.parquet").set_index("sku")
        except Exception:  # noqa: BLE001
            self.products = None
        try:
            self.inventory = self._read("inventory.parquet").set_index(["store", "sku"])
        except Exception:  # noqa: BLE001
            self.inventory = None
        self.loaded_at = time.time()

    def product(self, sku: int) -> dict | None:
        self.ensure()
        if self.products is None or sku not in self.products.index:
            return None
        return {k: (v.item() if hasattr(v, "item") else v) for k, v in self.products.loc[sku].items()}

    def stock(self, store: int, sku: int) -> dict | None:
        self.ensure()
        if self.inventory is None or (store, sku) not in self.inventory.index:
            return None
        r = self.inventory.loc[(store, sku)]
        return {k: (v.item() if hasattr(v, "item") else v) for k, v in r.items()}


def plan(forecast: dict, last_date: pd.Timestamp, shelf_life_days: int, lead_time_days: int, interval_days: int,
         case_pack: int, moq_cases: int, on_hand: float, in_transit: float, earliest_expiry=None,
         quantile: str | None = None) -> dict:
    """forecast: Chronos 返回的 {"0.1": [...], "0.5": [...], "0.9": [...]}，长度需 ≥ lead + D。"""
    L = max(0, int(lead_time_days))
    D = max(1, min(int(interval_days) + L, int(shelf_life_days) - L - 1))
    perishable = shelf_life_days <= PERISHABLE_DAYS
    q = quantile or ("0.5" if perishable else "0.9")
    seg = slice(L, L + D)
    demand = sum(max(0.0, v) for v in forecast[q][seg])
    demand_p50 = sum(max(0.0, v) for v in forecast["0.5"][seg])
    demand_p90 = sum(max(0.0, v) for v in forecast["0.9"][seg])
    cover_end = (last_date + pd.Timedelta(days=L + D)).date()
    expiring = False
    if earliest_expiry is not None and not pd.isna(earliest_expiry):
        expiring = pd.Timestamp(earliest_expiry).date() <= cover_end
    usable = (0.0 if expiring else float(on_hand)) + float(in_transit)
    need = max(0.0, demand - usable)
    cartons = max(int(moq_cases) if need > 0 else 0, math.ceil(need / case_pack)) if case_pack else 0
    order_units = cartons * case_pack
    leftover_after = usable + order_units - demand_p50       # 按 P50 卖，期末预计剩余
    return {
        "coverage": {"lead_time_days": L, "cover_days": D, "window": f"{(last_date + pd.Timedelta(days=L + 1)).date()} ~ {cover_end}",
                     "rule": f"覆盖天数 = min(补货间隔{interval_days} + 到货{L}, 保质期{shelf_life_days} − 到货{L} − 1) = {D}"},
        "service_level": {"quantile": q, "reason": "短保商品按 P50 备货，避免过期损耗" if perishable and q == "0.5"
                          else ("长保商品按 P90 备货，保障不缺货" if q == "0.9" else "用户指定")},
        "demand": {"used": round(demand, 1), "p50": round(demand_p50, 1), "p90": round(demand_p90, 1)},
        "stock": {"on_hand": on_hand, "in_transit": in_transit, "earliest_expiry": str(earliest_expiry) if earliest_expiry is not None else None,
                  "on_hand_usable": not expiring, "usable_total": round(usable, 1)},
        "order": {"units_needed": round(need, 1), "case_pack": case_pack, "cartons": cartons, "order_units": order_units},
        "risk": {"expected_leftover_units": round(leftover_after, 1),
                 "overstock_if_p90": round(max(0.0, demand_p90 - demand_p50), 1),
                 "note": ("现有库存在覆盖期内到期，已视为不可用" if expiring else "") +
                         ("；短保商品，若按 P90 备货预计多出 %.0f 件可能过期" % max(0.0, demand_p90 - demand_p50) if perishable else "")},
    }
