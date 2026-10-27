"""生成模拟的商品主数据与库存快照（客户尚未提供时用于演示补货决策）。

用法：python tools/mock_master_data.py --parquet data/sales_daily.parquet --out data/master [--upload s3://<DataBucket>/master/]

生成两张表（parquet + 同名 xlsx 便于查看）：
- products.parquet ：商品代码、品牌、品类、保质期(天)、到货周期(天)、补货间隔(天)、箱规、起订量(箱)、来源=mock
- inventory.parquet：门店、商品代码、现有库存、在途、最早到期日、快照日期
规则是按品牌关键词 + 价格推断品类，再按品类给保质期/周期区间，固定随机种子，可复现。
真实数据到位后用 iac/prepare_master.py 转换同结构的 Excel 覆盖即可。
"""
import argparse
import datetime as dt
from pathlib import Path

import boto3
import numpy as np
import pandas as pd

# 品类规则：(关键词列表, 品类, 保质期区间, 到货周期区间, 补货间隔, 箱规候选)
RULES = [
    (["桃李", "面包", "三明治", "便当", "饭团", "鲜食", "小每", "每一天"], "鲜食烘焙", (2, 5), (1, 1), 1, [6, 8, 10]),
    (["伊利", "蒙牛", "君乐宝", "新希望", "光明", "酸奶", "鲜奶"], "低温乳品", (7, 21), (1, 2), 2, [6, 12]),
    (["双汇", "火腿", "肉"], "肉制品", (30, 90), (2, 3), 7, [10, 20]),
    (["农夫山泉", "可口可乐", "康师傅", "怡宝", "百事", "脉动", "元气森林", "统一", "百岁山", "红牛", "三得利",
      "果子熟了", "娃哈哈", "东鹏", "雪碧", "芬达", "美汁源", "水"], "饮料", (180, 365), (2, 3), 7, [12, 15, 24]),
    (["青岛", "雪花", "哈尔滨", "百威", "啤酒", "白酒", "红酒"], "酒类", (365, 720), (3, 5), 7, [6, 12, 24]),
    (["好猫", "利群", "兰州", "南京", "云烟", "芙蓉王", "黄鹤楼", "中华", "玉溪", "黄金叶", "烟"], "烟草", (365, 365), (2, 3), 7, [10]),
    (["洁柔", "心相印", "维达", "清风", "纸", "牙", "洗"], "日用品", (730, 1095), (3, 5), 14, [6, 12]),
    (["盐津铺子", "卫龙", "喜之船", "随食", "乐事", "旺旺", "三只松鼠", "良品", "零食", "齐汇", "春蕾", "天香达", "珍享"],
     "零食", (120, 270), (2, 3), 7, [10, 20, 30]),
]
DEFAULT = ("其他", (90, 365), (2, 3), 7, [10, 12])


def classify(brand: str, price: float):
    b = "" if brand is None or (isinstance(brand, float) and np.isnan(brand)) or pd.isna(brand) else str(brand)
    price = None if price is None or pd.isna(price) else float(price)
    for kws, cat, shelf, lead, interval, packs in RULES:
        if any(k in b for k in kws):
            return cat, shelf, lead, interval, packs
    cat, shelf, lead, interval, packs = DEFAULT
    if price and price < 3:   # 低价无品牌商品多为鲜食/饮料类，保质期偏短
        return "其他", (30, 180), lead, interval, packs
    return cat, shelf, lead, interval, packs


def build(df: pd.DataFrame, seed: int = 42) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    last = df.date.max()
    prod = (df.sort_values("date").groupby("sku").agg(brand=("brand", "last") if "brand" in df else ("sku", "size"),
                                                      price=("price", "median") if "price" in df else ("qty", "size")).reset_index())
    rows = []
    for _, r in prod.iterrows():
        cat, shelf, lead, interval, packs = classify(r.get("brand"), r.get("price"))
        brand = r.get("brand") if "brand" in prod else None
        rows.append({"sku": int(r.sku), "brand": None if brand is None or pd.isna(brand) else str(brand), "category": cat,
                     "shelf_life_days": int(rng.integers(shelf[0], shelf[1] + 1)),
                     "lead_time_days": int(rng.integers(lead[0], lead[1] + 1)),
                     "replenish_interval_days": interval, "case_pack": int(rng.choice(packs)), "moq_cases": 1,
                     "source": "mock"})
    products = pd.DataFrame(rows)
    # 库存快照：近 30 天有销售的门店-商品
    recent = df[df.date > last - pd.Timedelta(days=30)].groupby(["store", "sku"])["qty"].sum().reset_index()
    recent["avg_daily"] = recent["qty"] / 30
    inv = recent.merge(products[["sku", "shelf_life_days", "case_pack"]], on="sku")
    cover = rng.uniform(1.5, 8.0, len(inv))
    inv["on_hand"] = np.maximum(0, np.round(inv["avg_daily"] * cover)).astype(int)
    inv["in_transit"] = np.where(rng.random(len(inv)) < 0.25, inv["case_pack"], 0).astype(int)
    exp_days = np.minimum(inv["shelf_life_days"], np.maximum(1, (rng.random(len(inv)) * inv["shelf_life_days"]).astype(int)))
    inv["earliest_expiry"] = [(last + pd.Timedelta(days=int(x))).date() for x in exp_days]
    inv["snapshot_date"] = last.date()
    inventory = inv[["store", "sku", "on_hand", "in_transit", "earliest_expiry", "snapshot_date"]].copy()
    inventory["source"] = "mock"
    return products, inventory


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True); ap.add_argument("--out", default="data/master")
    ap.add_argument("--upload", default=None, help="s3://bucket/master/"); ap.add_argument("--region", default="cn-northwest-1")
    a = ap.parse_args()
    df = pd.read_parquet(a.parquet)
    products, inventory = build(df)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    products.to_parquet(out / "products.parquet", index=False); inventory.to_parquet(out / "inventory.parquet", index=False)
    with pd.ExcelWriter(out / "商品主数据与库存快照-mock.xlsx", engine="openpyxl") as xw:
        products.rename(columns={"sku": "商品代码", "brand": "品牌", "category": "品类", "shelf_life_days": "保质期(天)",
                                 "lead_time_days": "到货周期(天)", "replenish_interval_days": "补货间隔(天)", "case_pack": "箱规",
                                 "moq_cases": "起订量(箱)", "source": "来源"}).to_excel(xw, index=False, sheet_name="商品主数据")
        inventory.rename(columns={"store": "门店代码", "sku": "商品代码", "on_hand": "现有库存", "in_transit": "在途",
                                  "earliest_expiry": "最早到期日", "snapshot_date": "快照日期", "source": "来源"}).to_excel(xw, index=False, sheet_name="库存快照")
    print(f"products: {len(products)} 行，品类分布 {products.category.value_counts().to_dict()}")
    print(f"inventory: {len(inventory)} 行（门店-商品），快照日期 {inventory.snapshot_date.iloc[0]}")
    if a.upload:
        b, prefix = a.upload[5:].split("/", 1); s3 = boto3.client("s3", region_name=a.region)
        for f in ("products.parquet", "inventory.parquet"):
            s3.put_object(Bucket=b, Key=prefix.rstrip("/") + "/" + f, Body=(out / f).read_bytes())
        print(f"已上传到 s3://{b}/{prefix.rstrip('/')}/")


if __name__ == "__main__":
    main()
