"""命令行批量预测：各店热销 Top-N 的未来 N 天销量 → Excel 报表。适合定时跑批（cron / EventBridge）。

直接调用 Chronos-2 预测服务（Runtime ②）的 batch_forecast，不经过大模型，一次调用完成。

示例：
  python tools/batch_forecast.py --excel 门店销售数据.xlsx --chronos-arn <ChronosRuntimeArn> --top 10 --days 7 --out top10.xlsx
  python tools/batch_forecast.py --parquet data/sales_daily.parquet --chronos-arn ... --stores 1 2 3 --out report.xlsx
  加 --upload s3://<bucket>/reports/ 可同时把报表放到 S3。
"""
import argparse
import asyncio
import datetime as dt
import json
import sys
import time
import urllib.parse
from pathlib import Path

import boto3
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_agent.batch import run_batch, summarize, to_excel_bytes  # noqa: E402
from iac.prepare_data import convert_excel  # noqa: E402


def runtime_url(arn: str, region: str) -> str:
    return (f"https://bedrock-agentcore.{region}.amazonaws.com.cn/runtimes/"
            f"{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT")


async def _batch_forecast_async(url: str, region: str, series: list[list[float]], days: int) -> list[dict]:
    from mcp import ClientSession
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    async with aws_iam_streamablehttp_client(endpoint=url, aws_region=region, aws_service="bedrock-agentcore") as (r, w, *_), \
            ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("batch_forecast", {"time_series": series, "prediction_length": days})
        data = res.structuredContent or json.loads(res.content[0].text)
        return data.get("result", data)


def make_batch_forecast(url: str, region: str):
    """给 run_batch 用的同步回调：每块序列开一个 MCP 会话调用 batch_forecast（通常只有一块）。"""
    def _bf(series: list[list[float]], days: int) -> list[dict]:
        t0 = time.time()
        out = asyncio.run(_batch_forecast_async(url, region, series, days))
        print(f"  batch_forecast {len(series)} 条序列 {time.time() - t0:.1f} s")
        return out
    return _bf


def main():
    ap = argparse.ArgumentParser(description="各店热销 Top-N 批量预测 → Excel")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--excel", help="客户销售 Excel")
    src.add_argument("--parquet", help="已转换的 parquet（本地路径或 s3://）")
    ap.add_argument("--chronos-arn", required=True, help="Chronos-2 预测服务 Runtime ARN（栈输出 ChronosRuntimeArn）")
    ap.add_argument("--region", default="cn-northwest-1")
    ap.add_argument("--stores", type=int, nargs="*", help="门店代码，默认全部")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--history-days", type=int, default=60)
    ap.add_argument("--out", default=None, help="输出 Excel 路径，默认 reports/top<N>_<日期>.xlsx")
    ap.add_argument("--upload", default=None, help="可选：s3://bucket/prefix/，同时上传报表")
    a = ap.parse_args()

    t0 = time.time()
    if a.excel:
        df = convert_excel(a.excel)
    elif a.parquet.startswith("s3://"):
        b, k = a.parquet[5:].split("/", 1)
        import io
        df = pd.read_parquet(io.BytesIO(boto3.client("s3", region_name=a.region).get_object(Bucket=b, Key=k)["Body"].read()))
    else:
        df = pd.read_parquet(a.parquet)
    print(f"数据：{len(df)} 行，截止 {df.date.max().date()}，{df.store.nunique()} 家门店")

    url = runtime_url(a.chronos_arn, a.region)
    report = run_batch(df, make_batch_forecast(url, a.region), a.stores, a.top, a.days, a.history_days)

    meta = {"生成时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "数据截止": str(df.date.max().date()),
            "门店": "全部" if not a.stores else ",".join(map(str, a.stores)), "每店 Top": a.top, "预测天数": a.days,
            "历史窗口": a.history_days, "模型": "Chronos-2（Amazon Bedrock AgentCore Runtime, CPU）",
            "口径": "P50 = 常规备货量；P90 = 安全库存上限"}
    out = Path(a.out or f"reports/top{a.top}_{a.days}d_{dt.date.today():%Y%m%d}.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(to_excel_bytes(report, meta))
    print(f"报表：{out}（{len(report)} 行，{time.time() - t0:.1f} s）")
    if a.upload:
        b, prefix = a.upload[5:].split("/", 1)
        key = prefix.rstrip("/") + "/" + out.name
        boto3.client("s3", region_name=a.region).put_object(Bucket=b, Key=key, Body=out.read_bytes(),
                                                           ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        print(f"已上传 s3://{b}/{key}")
    s = summarize(report, a.days)
    for st in s["stores"]:
        print(f"  门店 {st['store']}: {st['skus']} 个商品，未来{a.days}天 P50 合计 {st['p50_total']}，P90 合计 {st['p90_total']}")


if __name__ == "__main__":
    main()
