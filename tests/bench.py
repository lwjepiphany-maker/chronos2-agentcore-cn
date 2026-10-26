"""MCP 端到端测试：调用 3 个工具，记录延迟、精度（对比留出的真实值），并可与 SageMaker GPU 端点对比数值。

本地:     python tests/bench.py --url http://localhost:8000/mcp
Runtime:  python tests/bench.py --runtime-arn arn:aws-cn:bedrock-agentcore:... [--sagemaker chronos2-huimeijia]
"""
import argparse
import asyncio
import json
import time
import urllib.parse
from pathlib import Path

import numpy as np
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

PAYLOADS = Path(__file__).parent / "payloads"
CASES = ["single_30d", "covariates_60d", "batch_100", "batch_1000"]


def runtime_url(arn, region):
    return (f"https://bedrock-agentcore.{region}.amazonaws.com.cn/runtimes/"
            f"{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT")


def as_list(result):
    """MCP 工具返回 → 预测结果列表（单品返回 dict，批量返回 list）"""
    data = result.structuredContent
    if data is not None:
        data = data.get("result", data)
    else:
        data = json.loads(result.content[0].text)
    return data if isinstance(data, list) else [data]


def wape(preds, actuals):
    p = np.array([x["0.5"] for x in preds]); a = np.array(actuals, dtype=float)
    a = a.reshape(p.shape)
    return float(np.abs(p - a).sum() / max(a.sum(), 1e-9))


def sagemaker_predict(endpoint, region, case):
    import boto3
    sm = boto3.client("sagemaker-runtime", region_name=region)
    args = case["arguments"]
    if case["tool"] == "batch_forecast":
        inputs = [{"target": ts} for ts in args["time_series"]]
    else:
        inputs = [{k: v for k, v in {"target": args["history"],
                   "past_covariates": args.get("past_covariates"),
                   "future_covariates": args.get("future_covariates")}.items() if v}]
    body = {"inputs": inputs, "parameters": {"prediction_length": args["prediction_length"],
                                             "quantile_levels": [0.1, 0.5, 0.9]}}
    t0 = time.time()
    r = sm.invoke_endpoint(EndpointName=endpoint, ContentType="application/json",
                           Accept="application/json", Body=json.dumps(body))
    preds = json.loads(r["Body"].read())["predictions"]
    return preds, (time.time() - t0) * 1000


async def run(args):
    if args.runtime_arn:
        from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
        client = aws_iam_streamablehttp_client(endpoint=runtime_url(args.runtime_arn, args.region),
                                               aws_region=args.region, aws_service="bedrock-agentcore")
    else:
        client = streamablehttp_client(args.url)

    report = []
    t_conn = time.time()
    async with client as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            init_ms = (time.time() - t_conn) * 1000
            print(f"initialize + tools/list: {init_ms:.0f} ms  tools={[t.name for t in tools.tools]}")
            report.append({"step": "initialize+list_tools", "ms": round(init_ms)})

            for name in CASES:
                case = json.loads((PAYLOADS / f"{name}.json").read_text())
                lat = []
                for _ in range(args.repeat):
                    t0 = time.time()
                    res = await session.call_tool(case["tool"], case["arguments"])
                    lat.append((time.time() - t0) * 1000)
                    if res.isError:
                        raise RuntimeError(f"{name}: {res.content[0].text[:500]}")
                preds = as_list(res)
                row = {"case": name, "n_series": len(preds), "latency_ms": [round(x) for x in lat],
                       "wape_p50_vs_actual": round(wape(preds, case["actual"] if len(preds) > 1 else [case["actual"]]), 4)}
                if name.startswith("single") or name.startswith("cov"):
                    row["p50"] = [round(x, 2) for x in preds[0]["0.5"]]
                    row["actual"] = case["actual"]
                if args.sagemaker:
                    sm_preds, sm_ms = sagemaker_predict(args.sagemaker, args.region, case)
                    diff = max(float(np.max(np.abs(np.array(a[q]) - np.array(b[q]))))
                               for a, b in zip(preds, sm_preds) for q in ("mean", "0.1", "0.5", "0.9"))
                    row.update({"sagemaker_gpu_ms": round(sm_ms), "max_abs_diff_vs_gpu": diff})
                print(json.dumps(row, ensure_ascii=False))
                report.append(row)

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/mcp")
    p.add_argument("--runtime-arn")
    p.add_argument("--region", default="cn-northwest-1")
    p.add_argument("--sagemaker", help="SageMaker endpoint name for GPU comparison")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--out")
    asyncio.run(run(p.parse_args()))
