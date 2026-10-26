"""云上 Agent v2 测试：预测 + Code Interpreter 精确计算 + 画图，同一会话 3 轮。"""
import json, sys, time, uuid, boto3
ARN = sys.argv[1]; session = f"poc2-{uuid.uuid4().hex}"
rt = boto3.client("bedrock-agentcore", region_name="cn-northwest-1")
QS = ["预测5号店商品12080084下周销量",
      "现在库存120件，在途0，一箱12件，按P90安全库存算下周要订几箱？",
      "把这个商品最近30天销量和下周预测画成图"]
rows = []
for q in QS:
    t0 = time.time()
    r = rt.invoke_agent_runtime(agentRuntimeArn=ARN, qualifier="DEFAULT", runtimeSessionId=session, runtimeUserId="poc-user-001",
                                contentType="application/json", accept="application/json", payload=json.dumps({"prompt": q}).encode())
    body = json.loads(r["response"].read()); ms = round((time.time() - t0) * 1000)
    print(f"\n=== Q: {q}\n{body['result']}\n-- e2e {ms}ms | agent {body['total_ms']}ms | usage {body['usage']} | tools {body['tools']}")
    rows.append({"q": q, "e2e_ms": ms, "agent_ms": body["total_ms"], "usage": body["usage"], "tools": body["tools"], "result": body["result"]})
json.dump({"session": session, "turns": rows}, open("results_agent_cloud_v2.json", "w"), ensure_ascii=False, indent=1)
print("\nsession:", session)
