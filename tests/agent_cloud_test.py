"""云上 Agent 端到端测试：同一会话 3 轮对话，记录延迟/token/工具调用。"""
import json, sys, time, uuid, boto3
ARN = sys.argv[1]
session = f"poc-{uuid.uuid4().hex}"
rt = boto3.client("bedrock-agentcore", region_name="cn-northwest-1")
QS = ["预测5号店商品12080084下周销量",
      "5号店最近30天卖得最好的3个商品是什么？分别预测它们下周的销量，并给出补货建议",
      "刚才第一个商品最近两周每天卖多少？"]
rows = []
for q in QS:
    t0 = time.time()
    r = rt.invoke_agent_runtime(agentRuntimeArn=ARN, qualifier="DEFAULT", runtimeSessionId=session, runtimeUserId="poc-user-001",
                                contentType="application/json", accept="application/json",
                                payload=json.dumps({"prompt": q}).encode())
    body = json.loads(r["response"].read()); ms = round((time.time() - t0) * 1000)
    print(f"\n=== Q: {q}\n{body['result']}\n-- e2e {ms}ms | agent {body['total_ms']}ms | usage {body['usage']} | tools {body['tools']}")
    rows.append({"q": q, "e2e_ms": ms, "agent_ms": body["total_ms"], "usage": body["usage"], "tools": body["tools"]})
json.dump({"session": session, "turns": rows}, open("results_agent_cloud.json", "w"), ensure_ascii=False, indent=1)
