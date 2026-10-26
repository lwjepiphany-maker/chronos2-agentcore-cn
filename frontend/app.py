"""补货助手 Web 前端（Streamlit）。

运行在使用者本机或贵司内网服务器上，用 AWS 凭证调用 AgentCore Runtime：
    streamlit run frontend/app.py -- --agent-arn <AGENT_RUNTIME_ARN> --region cn-northwest-1

- 每个浏览器会话对应一个 Runtime 会话（runtimeSessionId），Agent 记住上下文；点"新会话"重置
- Agent 返回的图表（s3://.../charts/*.png）由前端用同一凭证从 S3 下载后内嵌显示，不走公网链接
- 右侧显示本轮工具调用、token 用量、耗时，便于演示讲解
"""
import argparse
import json
import re
import sys
import time
import uuid

import boto3
import streamlit as st

parser = argparse.ArgumentParser()
parser.add_argument("--agent-arn", required=True)
parser.add_argument("--region", default="cn-northwest-1")
parser.add_argument("--profile", default=None)
args, _ = parser.parse_known_args(sys.argv[1:])

st.set_page_config(page_title="销量预测补货助手", page_icon="📦", layout="wide")

session = boto3.Session(profile_name=args.profile, region_name=args.region)
rt = session.client("bedrock-agentcore")
s3 = session.client("s3")

S3_PNG = re.compile(r"s3://([a-z0-9.\-]+)/(\S+?\.png)")
EXAMPLES = ["预测5号店商品12080084下周销量",
            "5号店最近30天卖得最好的3个商品，分别预测下周销量并给补货建议",
            "考虑天气的话，5号店商品12080084下周销量和不考虑天气比有什么差别？",
            "现在库存120件，在途0，一箱12件，按P90安全库存算要订几箱？",
            "把这个商品最近30天销量和下周预测画成图"]

if "session_id" not in st.session_state:
    st.session_state.session_id = f"web-{uuid.uuid4().hex}"
    st.session_state.messages = []
    st.session_state.user_id = "store-ops-001"


def invoke(prompt: str) -> dict:
    r = rt.invoke_agent_runtime(agentRuntimeArn=args.agent_arn, qualifier="DEFAULT",
                                runtimeSessionId=st.session_state.session_id, runtimeUserId=st.session_state.user_id,
                                contentType="application/json", accept="application/json",
                                payload=json.dumps({"prompt": prompt}).encode())
    return json.loads(r["response"].read())


def fetch_charts(text: str) -> list[bytes]:
    imgs = []
    for bucket, key in S3_PNG.findall(text):
        try:
            imgs.append(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception as e:  # noqa: BLE001
            st.warning(f"图表下载失败 {bucket}/{key}: {e}")
    return imgs


with st.sidebar:
    st.title("📦 补货助手")
    st.caption("Chronos-2 × Amazon Bedrock AgentCore · 宁夏区域")
    st.text_input("用户标识（runtimeUserId）", key="user_id")
    st.code(st.session_state.session_id, language=None)
    if st.button("🔄 新会话", use_container_width=True):
        st.session_state.session_id = f"web-{uuid.uuid4().hex}"
        st.session_state.messages = []
        st.rerun()
    st.divider()
    st.markdown("**试试这些问题**")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True):
            st.session_state.pending = ex
    st.divider()
    st.markdown(f"Region `{args.region}`  \nAgent `…{args.agent_arn[-32:]}`")

st.markdown("### 门店销量预测与补货建议")
for m in st.session_state.messages:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        for img in m.get("images", []):
            st.image(img)
        if m.get("meta"):
            with st.expander("调用详情", expanded=False):
                st.json(m["meta"])

prompt = st.chat_input("例如：预测5号店商品12080084下周销量") or st.session_state.pop("pending", None)
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Agent 正在取数、预测、计算…"):
            t0 = time.time()
            try:
                body = invoke(prompt)
                text = body.get("result", "")
                imgs = fetch_charts(text)
                meta = {"端到端耗时 ms": round((time.time() - t0) * 1000), "Agent 耗时 ms": body.get("total_ms"),
                        "工具调用（会话累计）": body.get("tools"), "token": body.get("usage")}
            except Exception as e:  # noqa: BLE001
                text, imgs, meta = f"调用失败：{e}", [], None
        st.markdown(text)
        for img in imgs:
            st.image(img)
        if meta:
            with st.expander("调用详情", expanded=False):
                st.json(meta)
    st.session_state.messages.append({"role": "assistant", "content": text, "images": imgs, "meta": meta})
