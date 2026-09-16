"""接口冒烟测试：验证百炼 API Key 有效、三个模型都能调通。

为什么要先跑这个：把「Key 填错了」「模型名写错了」「余额不足」「网络不通」
这类环境问题，在写业务代码之前就暴露出来。否则写完几百行代码才发现调不通，
排查时会误以为是自己的代码有问题，白白浪费时间。

运行方式（在项目根目录下）：
    backend/.venv/Scripts/python.exe scripts/smoke_test_api.py
"""

import os
import sys
import time
from pathlib import Path

# 把 backend 目录加进模块搜索路径，这样才能 import app 下的东西
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_DIR / ".env")

PASS = "[通过]"
FAIL = "[失败]"

failures = []


def section(title: str) -> None:
    print()
    print("=" * 62)
    print(f"  {title}")
    print("=" * 62)


def check(ok: bool, detail: str) -> None:
    print(f"{PASS if ok else FAIL} {detail}")
    if not ok:
        failures.append(detail)


# ============================================================
section("0. 检查配置")
# ============================================================
api_key = os.getenv("DASHSCOPE_API_KEY", "")
if not api_key or api_key.startswith("sk-在这里"):
    print(f"{FAIL} 没有配置 DASHSCOPE_API_KEY")
    print("       请打开 backend/.env，把 DASHSCOPE_API_KEY 填成你自己的百炼 API Key")
    sys.exit(1)

print(f"{PASS} API Key 已配置：{api_key[:8]}...{api_key[-4:]}（共 {len(api_key)} 字符）")
print(f"       对话模型   ：{os.getenv('LLM_MODEL')}")
print(f"       向量模型   ：{os.getenv('EMBEDDING_MODEL')}")
print(f"       重排模型   ：{os.getenv('RERANK_MODEL')}")


# ============================================================
section("1. 测试向量化模型（text-embedding-v3）")
# ============================================================
try:
    import dashscope

    t0 = time.time()
    resp = dashscope.TextEmbedding.call(
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
        input=["这款手机的电池容量是 5000mAh", "支持 67W 快充"],
        dimension=int(os.getenv("EMBEDDING_DIM", "1024")),
        api_key=api_key,
    )
    elapsed = time.time() - t0

    if resp.status_code == 200:
        embeddings = resp.output["embeddings"]
        dim = len(embeddings[0]["embedding"])
        check(True, f"调用成功，耗时 {elapsed:.2f}s")
        check(True, f"返回 {len(embeddings)} 条向量，维度 = {dim}")
        expected_dim = int(os.getenv("EMBEDDING_DIM", "1024"))
        check(
            dim == expected_dim,
            f"维度与配置一致（期望 {expected_dim}，实际 {dim}）",
        )
        # 顺手验证一下向量内容是否正常：两个语义不同的句子，向量不该完全相同
        v0 = embeddings[0]["embedding"]
        v1 = embeddings[1]["embedding"]
        check(v0 != v1, "不同文本产生了不同的向量（说明真的在算，不是返回固定值）")
    else:
        check(False, f"调用失败：code={resp.status_code} message={resp.message}")
except Exception as e:
    check(False, f"异常：{type(e).__name__}: {e}")


# ============================================================
section("2. 测试重排模型（gte-rerank-v2）")
# ============================================================
try:
    t0 = time.time()
    resp = dashscope.TextReRank.call(
        model=os.getenv("RERANK_MODEL", "gte-rerank-v2"),
        query="手机电池多大",
        documents=[
            "这款手机搭载 5000mAh 大容量电池，支持 67W 有线快充。",
            "本店支持七天无理由退货，商品需保持完好。",
            "手机屏幕为 6.7 英寸 AMOLED，分辨率 2712×1220。",
        ],
        top_n=3,
        return_documents=False,
        api_key=api_key,
    )
    elapsed = time.time() - t0

    if resp.status_code == 200:
        results = resp.output["results"]
        check(True, f"调用成功，耗时 {elapsed:.2f}s")
        check(True, f"返回 {len(results)} 条排序结果")
        print()
        print("       排序结果（分数越高越相关）：")
        for r in results:
            print(f"         [{r['index']}] 相关度 {r['relevance_score']:.4f}")
        # 最相关的那条应该是讲电池的（索引 0），这是最直观的正确性验证
        top_index = results[0]["index"]
        check(
            top_index == 0,
            f"最相关的是「电池」那条（实际返回索引 {top_index}，期望 0）",
        )
    else:
        check(False, f"调用失败：code={resp.status_code} message={resp.message}")
except Exception as e:
    check(False, f"异常：{type(e).__name__}: {e}")


# ============================================================
section("3. 测试对话模型（qwen-plus）")
# ============================================================
try:
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=os.getenv("LLM_MODEL", "qwen-plus"),
        api_key=api_key,
        base_url=os.getenv(
            "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        temperature=0.1,
        timeout=60,
    )

    # 3.1 一次性返回
    t0 = time.time()
    resp = llm.invoke("用一句话说明什么是 RAG，不要超过 40 个字。")
    elapsed = time.time() - t0

    check(True, f"一次性调用成功，耗时 {elapsed:.2f}s")
    print(f"       模型回复：{resp.content.strip()}")

    usage = getattr(resp, "usage_metadata", None) or {}
    if usage:
        print(
            f"       token 用量：输入 {usage.get('input_tokens', '?')}，"
            f"输出 {usage.get('output_tokens', '?')}"
        )

    # 3.2 流式返回 —— 后面的问答功能全靠这个，必须验证
    t0 = time.time()
    first_token_time = None
    chunks = []
    for chunk in llm.stream("数一下 1 到 5。"):
        if first_token_time is None:
            first_token_time = time.time() - t0
        if chunk.content:
            chunks.append(chunk.content)

    check(len(chunks) > 1, f"流式调用成功，收到 {len(chunks)} 个数据块（真流式，非一次性返回）")
    check(
        first_token_time is not None and first_token_time < elapsed,
        f"首字延迟 {first_token_time:.2f}s，快于一次性返回的 {elapsed:.2f}s",
    )
    print(f"       流式拼接结果：{''.join(chunks).strip()}")
except Exception as e:
    check(False, f"异常：{type(e).__name__}: {e}")


# ============================================================
section("测试结论")
# ============================================================
if failures:
    print(f"{FAIL} 有 {len(failures)} 项未通过：")
    for f in failures:
        print(f"       - {f}")
    print()
    print("请根据上面的提示排查。常见原因：API Key 填错、百炼账号余额不足、网络不通。")
    sys.exit(1)
else:
    print(f"{PASS} 三个模型全部调通，可以开始 M2 的开发了。")
    print()
    print("       - 向量化 text-embedding-v3  → 用于把文档切块转成向量")
    print("       - 重排   gte-rerank-v2      → 用于对召回结果精排")
    print("       - 对话   qwen-plus          → 用于生成回答（支持流式）")
    sys.exit(0)
