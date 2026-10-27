"""把客户的商品主数据 / 库存快照 Excel 转成 Agent 读取的 parquet。

用法：python iac/prepare_master.py <商品主数据与库存.xlsx> data/master   （再 aws s3 cp data/master/*.parquet s3://<DataBucket>/master/）

Excel 两个 sheet（名字不限，按列识别）：
- 商品主数据：商品代码、保质期(天)、到货周期(天)、补货间隔(天)、箱规、起订量(箱)，可选：品牌、品类
- 库存快照：门店代码、商品代码、现有库存、在途、最早到期日、快照日期
"""
import os
import sys

import pandas as pd

PROD_MAP = {"商品代码": "sku", "品牌": "brand", "品类": "category", "保质期(天)": "shelf_life_days", "保质期": "shelf_life_days",
            "到货周期(天)": "lead_time_days", "到货周期": "lead_time_days", "补货间隔(天)": "replenish_interval_days",
            "补货间隔": "replenish_interval_days", "箱规": "case_pack", "起订量(箱)": "moq_cases", "起订量": "moq_cases"}
INV_MAP = {"门店代码": "store", "门代码": "store", "商品代码": "sku", "现有库存": "on_hand", "库存": "on_hand", "在途": "in_transit",
           "最早到期日": "earliest_expiry", "快照日期": "snapshot_date"}


def convert_master(src):
    sheets = pd.read_excel(src, sheet_name=None)
    products = inventory = None
    for df in sheets.values():
        cols = set(df.columns)
        if {"商品代码", "箱规"} <= cols or {"商品代码", "保质期(天)"} <= cols:
            p = df.rename(columns=PROD_MAP)
            for c, d in (("lead_time_days", 2), ("replenish_interval_days", 7), ("moq_cases", 1), ("case_pack", 1), ("shelf_life_days", 365)):
                p[c] = pd.to_numeric(p.get(c), errors="coerce").fillna(d).astype(int)
            p["sku"] = pd.to_numeric(p["sku"], errors="coerce"); p = p.dropna(subset=["sku"]); p["sku"] = p["sku"].astype(int)
            p["source"] = "customer"
            products = p[[c for c in ("sku", "brand", "category", "shelf_life_days", "lead_time_days", "replenish_interval_days", "case_pack", "moq_cases", "source") if c in p]]
        elif {"商品代码", "现有库存"} <= cols or {"商品代码", "库存"} <= cols:
            i = df.rename(columns=INV_MAP)
            for c in ("store", "sku", "on_hand"):
                i[c] = pd.to_numeric(i[c], errors="coerce")
            i = i.dropna(subset=["store", "sku", "on_hand"])
            i["in_transit"] = pd.to_numeric(i.get("in_transit"), errors="coerce").fillna(0).astype(int)
            i["earliest_expiry"] = pd.to_datetime(i.get("earliest_expiry"), errors="coerce").dt.date
            i["snapshot_date"] = pd.to_datetime(i.get("snapshot_date"), errors="coerce").dt.date
            i["source"] = "customer"
            inventory = i[["store", "sku", "on_hand", "in_transit", "earliest_expiry", "snapshot_date", "source"]].astype({"store": int, "sku": int, "on_hand": int})
    if products is None:
        raise ValueError("没有找到商品主数据 sheet（需含 商品代码、箱规 或 保质期(天)）")
    return products, inventory


if __name__ == "__main__":
    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    products, inventory = convert_master(src)
    products.to_parquet(os.path.join(out, "products.parquet"), index=False); print(f"products {len(products)} 行")
    if inventory is not None:
        inventory.to_parquet(os.path.join(out, "inventory.parquet"), index=False); print(f"inventory {len(inventory)} 行")
