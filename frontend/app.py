"""补货助手 Web 前端（Streamlit）。

运行在使用者本机或贵司内网服务器上，用 AWS 凭证调用 AgentCore Runtime：
    streamlit run frontend/app.py -- --agent-arn <AGENT_RUNTIME_ARN> --data-bucket <DataBucket> --region cn-northwest-1

- 每个浏览器会话对应一个 Runtime 会话（runtimeSessionId），Agent 记住上下文；点"新会话"重置
- 提问在后台线程执行，期间可点"停止"：会结束云端会话（StopRuntimeSession）并开启新会话，不再等待回答
- "撤回并修改"把上一条问题放回编辑框，改完重发
- Agent 返回的图表 / Excel 报表由前端用同一凭证从 S3 取回显示 / 提供下载
- 侧栏可上传销售 Excel 覆盖数据，Agent 30 秒内热加载
"""
import argparse
import json
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
import streamlit as st

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from iac.prepare_data import convert_excel, describe, to_parquet_bytes  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--agent-arn", required=True)
parser.add_argument("--region", default="cn-northwest-1")
parser.add_argument("--profile", default=None)
parser.add_argument("--data-bucket", default=None, help="数据桶名（栈输出 DataBucket），用于上传 Excel 和下载报表")
parser.add_argument("--data-key", default="data/sales_daily.parquet")
args, _ = parser.parse_known_args(sys.argv[1:])

st.set_page_config(page_title="销量预测补货助手", page_icon="📦", layout="wide")

session = boto3.Session(profile_name=args.profile, region_name=args.region)
rt = session.client("bedrock-agentcore")
s3 = session.client("s3")


@st.cache_resource(show_spinner=False)
def executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=4)


@st.cache_data(ttl=600, show_spinner=False)
def runtime_model_info(arn: str) -> dict:
    """从 Runtime 配置读大模型信息（需要 bedrock-agentcore:GetAgentRuntime 权限）；没权限时返回空，改用响应里的 model 字段。"""
    try:
        rid = arn.rsplit("/", 1)[-1]
        env = session.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=rid).get("environmentVariables", {})
        return {"model": env.get("LLM_MODEL"), "endpoint": env.get("LLM_BASE_URL", "").split("//")[-1].split("/")[0]}
    except Exception:  # noqa: BLE001
        return {}


S3_PNG = re.compile(r"s3://([a-z0-9.\-]+)/(\S+?\.png)")
S3_XLSX = re.compile(r"s3://([a-z0-9.\-]+)/(\S+?\.xlsx)")
EXAMPLES = ["预测5号店商品12070007下周销量",
            "这个商品要补多少货？",
            "下周五到周日做促销价，销量会怎样？和不促销比",
            "5号店热销Top10最近几周预测准不准？",
            "所有门店热销Top10商品下周销量批量预测，生成报表",
            "把这个商品最近30天销量和下周预测画成图"]
GLOSSARY = {
    "预计销量": "最可能的销量（统计上的中位数 P50）：一半的可能卖得更多，一半更少。日常备货按它来。",
    "最多备货量 / 安全上限": "有 9 成把握不会卖超的量（P90）。怕缺货、商品不易过期时按它备。",
    "保守估计": "有 9 成把握至少能卖到的量（P10）。",
    "准确率": "历史回测里 1 − 误差率（WAPE）。76% 表示过去几周预测总量与实际相差约 24%。",
    "覆盖天数": "这次补的货要撑到下次到货为止的天数，受保质期限制。",
}


def new_session():
    st.session_state.session_id = f"web-{uuid.uuid4().hex}"
    st.session_state.messages = []
    st.session_state.pop("future", None)
    st.session_state.pop("pending", None)
    st.session_state.prefill = None


if "session_id" not in st.session_state:
    new_session()
    st.session_state.user_id = "store-ops-001"


def invoke(prompt: str, session_id: str, user_id: str) -> dict:
    r = rt.invoke_agent_runtime(agentRuntimeArn=args.agent_arn, qualifier="DEFAULT",
                                runtimeSessionId=session_id, runtimeUserId=user_id,
                                contentType="application/json", accept="application/json",
                                payload=json.dumps({"prompt": prompt}).encode())
    return json.loads(r["response"].read())


def stop_session(session_id: str):
    """结束云端会话：正在跑的回答随之终止，不再计费。"""
    try:
        rt.stop_runtime_session(agentRuntimeArn=args.agent_arn, qualifier="DEFAULT", runtimeSessionId=session_id)
    except Exception as e:  # noqa: BLE001
        st.toast(f"停止会话时云端返回：{e}", icon="⚠️")


def fetch_files(text: str):
    imgs, files = [], []
    for bucket, key in S3_PNG.findall(text):
        try:
            imgs.append(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        except Exception as e:  # noqa: BLE001
            st.warning(f"图表下载失败 {bucket}/{key}: {e}")
    for bucket, key in S3_XLSX.findall(text):
        try:
            files.append((key.rsplit("/", 1)[-1], s3.get_object(Bucket=bucket, Key=key)["Body"].read()))
        except Exception as e:  # noqa: BLE001
            st.warning(f"报表下载失败 {bucket}/{key}: {e}")
    return imgs, files


busy = "future" in st.session_state

# ----------------------------------------------------------------- 侧栏
with st.sidebar:
    st.title("📦 补货助手")
    st.caption("Chronos-2 × Amazon Bedrock AgentCore · 宁夏区域")
    st.text_input("用户标识（runtimeUserId）", key="user_id")
    st.code(st.session_state.session_id, language=None)
    if st.button("🔄 新会话", use_container_width=True):
        if busy:
            stop_session(st.session_state.session_id)
        new_session()
        st.rerun()
    st.divider()
    st.markdown("**更新销售数据**")
    if args.data_bucket:
        up = st.file_uploader("上传销售 Excel（覆盖当前数据，S3 保留历史版本可回退）", type=["xlsx", "xls"])
        if up is not None and st.button("转换并上传", use_container_width=True):
            try:
                with st.spinner("转换并上传中…"):
                    df = convert_excel(up)
                    s3.put_object(Bucket=args.data_bucket, Key=args.data_key, Body=to_parquet_bytes(df))
                st.success(f"已上传：{describe(df)}。Agent 会在 30 秒内自动使用新数据。")
            except Exception as e:  # noqa: BLE001
                st.error(f"上传失败：{e}")
    else:
        st.caption("启动时加 `--data-bucket <桶名>` 可在此上传 Excel")
    st.divider()
    st.markdown("**试试这些问题**")
    for ex in EXAMPLES:
        if st.button(ex, use_container_width=True, disabled=busy):
            st.session_state.pending = ex
    st.divider()
    with st.expander("名词解释"):
        for k, v in GLOSSARY.items():
            st.markdown(f"**{k}**：{v}")
    info = runtime_model_info(args.agent_arn) or st.session_state.get("model_info", {})
    st.markdown("**大模型**")
    if info.get("model"):
        st.markdown(f"`{info['model']}`  \n接口：`{info.get('endpoint', '')}`")
    else:
        st.caption("首轮对话后显示")
    st.markdown("**预测模型**  \n`Chronos-2`（CPU，AgentCore Runtime）")
    st.markdown(f"Region `{args.region}`  \nAgent `…{args.agent_arn[-32:]}`")

# ----------------------------------------------------------------- 历史消息
st.markdown("### 门店销量预测与补货建议")
for i, m in enumerate(st.session_state.messages):
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        for img in m.get("images", []):
            st.image(img)
        for j, (name, data) in enumerate(m.get("files", [])):
            st.download_button(f"下载报表 {name}", data, file_name=name, key=f"dl-{i}-{j}")
        if m.get("meta"):
            with st.expander("调用详情", expanded=False):
                st.json(m["meta"])

# ----------------------------------------------------------------- 进行中的请求：停止按钮 + 轮询
if busy:
    fut, started, prompt_running = st.session_state.future, st.session_state.future_started, st.session_state.future_prompt
    with st.chat_message("assistant"):
        c1, c2 = st.columns([5, 1])
        c1.info(f"Agent 正在处理（{int(time.time() - started)} 秒）：取数、预测、计算…")
        if c2.button("⏹ 停止", type="primary", use_container_width=True):
            stop_session(st.session_state.session_id)
            del st.session_state.future
            st.session_state.messages.append({"role": "assistant", "content": "（已停止。云端会话已结束，下一条提问会开启新会话。）"})
            st.session_state.session_id = f"web-{uuid.uuid4().hex}"
            st.session_state.prefill = prompt_running
            st.rerun()
    if fut.done():
        try:
            body = fut.result()
            text = body.get("result", "")
            if body.get("model"):
                st.session_state.model_info = {"model": body["model"], "endpoint": body.get("model_endpoint", "")}
            imgs, files = fetch_files(text)
            meta = {"大模型": body.get("model"), "端到端耗时 ms": round((time.time() - started) * 1000), "Agent 耗时 ms": body.get("total_ms"),
                    "工具调用（会话累计）": body.get("tools"), "token": body.get("usage")}
        except Exception as e:  # noqa: BLE001
            text, imgs, files, meta = f"调用失败：{e}", [], [], None
        st.session_state.messages.append({"role": "assistant", "content": text, "images": imgs, "files": files, "meta": meta})
        del st.session_state.future
        st.rerun()
    else:
        time.sleep(1)
        st.rerun()

# ----------------------------------------------------------------- 撤回并修改
if st.session_state.get("prefill") and not busy:
    with st.expander("修改并重发上一条问题", expanded=True):
        edited = st.text_area("问题", value=st.session_state.prefill, key="prefill_box")
        c1, c2 = st.columns([1, 1])
        if c1.button("发送修改后的问题", type="primary"):
            st.session_state.pending = edited
            st.session_state.prefill = None
            st.rerun()
        if c2.button("放弃"):
            st.session_state.prefill = None
            st.rerun()

# ----------------------------------------------------------------- 输入
last_user = next((m for m in reversed(st.session_state.messages) if m["role"] == "user"), None)
if last_user and not busy and not st.session_state.get("prefill"):
    if st.button("↩ 撤回并修改上一条"):
        # 删掉最后一问一答，把问题放回编辑框；云端会话里这轮仍存在，所以同时开新会话避免上下文混淆
        idx = st.session_state.messages.index(last_user)
        st.session_state.messages = st.session_state.messages[:idx]
        st.session_state.prefill = last_user["content"]
        st.session_state.session_id = f"web-{uuid.uuid4().hex}"
        st.rerun()

prompt = st.chat_input("例如：预测5号店商品12070007下周销量", disabled=busy) or st.session_state.pop("pending", None)
if prompt and not busy:
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state.future = executor().submit(invoke, prompt, st.session_state.session_id, st.session_state.user_id)
    st.session_state.future_started = time.time()
    st.session_state.future_prompt = prompt
    st.rerun()
