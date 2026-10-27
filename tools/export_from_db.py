"""从客户数据库导出日销量 → parquet → S3（供定时任务使用，替代手工上传 Excel）。

Agent 只读 S3 上的 data/sales_daily.parquet，并按 ETag 自动热加载，所以数据库到 S3 的管道可以独立运行：
  数据库（MySQL / SQL Server / PostgreSQL …） ──每日 SQL 导出──▶ parquet ──▶ s3://<DataBucket>/data/sales_daily.parquet

用法：
  pip install sqlalchemy pymysql pandas pyarrow boto3      # 按数据库类型换驱动：psycopg2 / pymssql
  export DB_URL='mysql+pymysql://user:pass@host:3306/retail'
  python tools/export_from_db.py --sql sql/daily_sales.sql --days 120 --upload s3://<DataBucket>/data/sales_daily.parquet

SQL 需要返回与 prepare_data.py 相同的中文列名（见 docs/销售数据格式说明.md）：
  记账日期, 门店代码, 商品代码, 销售数量 为必需；门店类型, 天气, 温度, 星期, 节假日, 促销类型, 促销数量, 优惠金额, 促销单据类别, 门店售价, 商品品牌, 门店商品ABC属性 可选。
示例 SQL（按客户表结构改写）：
  SELECT sale_date AS 记账日期, store_id AS 门店代码, sku AS 商品代码, SUM(qty) AS 销售数量,
         MAX(price) AS 门店售价, MAX(promo_type) AS 促销类型, SUM(promo_qty) AS 促销数量, SUM(discount) AS 优惠金额
  FROM sales_daily WHERE sale_date >= :start_date GROUP BY sale_date, store_id, sku
生产上可放到 EventBridge 定时 + Lambda / Glue Python Shell，或客户现有的调度平台。
"""
import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iac.prepare_data import REQUIRED, convert_excel, describe, to_parquet_bytes  # noqa: E402


def frame_to_parquet_bytes(df: pd.DataFrame) -> bytes:
    """复用 Excel 转换器的清洗逻辑：先写成内存 Excel 再转，保证两条路径口径完全一致。"""
    import io
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    out = convert_excel(buf)
    print(describe(out))
    return to_parquet_bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sql", required=True, help="SQL 文件，可用 :start_date 参数")
    ap.add_argument("--days", type=int, default=120, help="导出最近多少天")
    ap.add_argument("--upload", required=True, help="s3://bucket/data/sales_daily.parquet")
    ap.add_argument("--region", default="cn-northwest-1")
    a = ap.parse_args()
    from sqlalchemy import create_engine, text
    import boto3
    engine = create_engine(os.environ["DB_URL"])
    start = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()
    with engine.connect() as conn:
        df = pd.read_sql(text(Path(a.sql).read_text(encoding="utf-8")), conn, params={"start_date": start})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise SystemExit(f"SQL 结果缺少必需列 {missing}")
    data = frame_to_parquet_bytes(df)
    b, k = a.upload[5:].split("/", 1)
    boto3.client("s3", region_name=a.region).put_object(Bucket=b, Key=k, Body=data)
    print(f"已上传 {a.upload}（{len(data) // 1024} KB），Agent 将在 30 秒内自动加载")


if __name__ == "__main__":
    main()
