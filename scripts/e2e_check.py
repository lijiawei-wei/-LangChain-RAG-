"""M7 端到端验收：点赞点踩 + 统计看板。

跑法（后端要在 8000 端口上开着）：
    .venv/Scripts/python.exe ../scripts/e2e_check.py

为什么单独写这么个脚本，而不是全放进 pytest：
pytest 里跑的是把流水线换成假实现的单元测试，快、不花钱，
但验证不了"这套东西在真实数据上到底通不通"。
这个脚本走的是**真的接口、真的知识库、真的模型调用**，
用来做交付前的最后一道确认。
"""

import io
import json
import sys
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

BASE = "http://127.0.0.1:8000/api/v1"
passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [OK]   {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")
    return condition


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    """发一个请求，返回 (状态码, 响应体)。"""
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


def login(username: str, password: str) -> str | None:
    status, body = call("POST", "/auth/login", body={"username": username, "password": password})
    if status == 200:
        return body["data"]["access_token"]
    return None


print("=" * 60)
print("M7 端到端验收")
print("=" * 60)

# ---------- 0. 准备账号 ----------
print("\n[0] 准备账号")
admin_token = login("admin", "123456")
check("管理员登录成功", admin_token is not None)

USER = "e2e_tester"
PASSWORD = "e2e123456"
call("POST", "/auth/register", body={"username": USER, "password": PASSWORD})
user_token = login(USER, PASSWORD)
check("普通用户登录成功", user_token is not None)

if not admin_token or not user_token:
    print("\n账号没准备好，后面的测试没法继续")
    sys.exit(1)

# ---------- 1. 权限隔离 ----------
print("\n[1] 权限隔离")
status, _ = call("GET", "/admin/stats/overview", user_token)
check("普通用户访问统计看板被拒（403）", status == 403, f"实际 {status}")

status, _ = call("GET", "/admin/stats/overview")
check("未登录访问统计看板被拒（401）", status == 401, f"实际 {status}")

status, _ = call("GET", "/stats/me", user_token)
check("普通用户可以看自己的统计", status == 200, f"实际 {status}")

# ---------- 2. 真实问答 ----------
print("\n[2] 真实问答（会调用模型，产生费用）")
status, kb_body = call("GET", "/knowledge-bases/all", admin_token)
kbs = kb_body.get("data", []) if status == 200 else []
check("能读到知识库列表", len(kbs) > 0, "知识库里没有数据，请先上传资料")

if not kbs:
    print("\n没有知识库，跳过问答相关验证")
    print(f"\n通过 {passed}，失败 {failed}")
    sys.exit(0)

kb_id = kbs[0]["id"]
status, body = call(
    "POST",
    "/chat/query",
    user_token,
    {"question": "电池容量是多少？", "kb_id": kb_id},
)
check("提问成功", status == 200, f"实际 {status}: {body.get('message')}")

if status != 200:
    print(f"\n通过 {passed}，失败 {failed}")
    sys.exit(1)

data = body["data"]
assistant_id = data["assistant_message_id"]
conversation_id = data["conversation_id"]
check("回答里带了引用", len(data["citations"]) > 0)
print(f"         分阶段耗时：{data.get('stage_latency')}")

# ---------- 2.5 流式问答 ----------
# 单独测一遍流式路径，因为它是用户在浏览器上真正走的那条路，
# 而且「首字延迟」只有这条路才会记录（非流式本来就没有"第一个字"这个概念，
# 它是一次性把整段回答返回的）。
print("\n[2.5] 流式问答（SSE）")
stream_url = f"{BASE}/chat/stream"
stream_body = json.dumps({"question": "这款手机的屏幕尺寸是多少？", "kb_id": kb_id}).encode()
stream_req = urllib.request.Request(stream_url, data=stream_body, method="POST")
stream_req.add_header("Content-Type", "application/json")
stream_req.add_header("Authorization", f"Bearer {user_token}")

events = []
try:
    with urllib.request.urlopen(stream_req, timeout=120) as resp:
        content_type = resp.headers.get("Content-Type", "")
        check("返回的是 SSE 流", "text/event-stream" in content_type, content_type)
        check("带上了防缓冲头（否则 nginx 下会变成假流式）",
              resp.headers.get("X-Accel-Buffering") == "no")

        buffer = ""
        for raw in resp:
            buffer += raw.decode("utf-8")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                name = None
                payload = None
                for line in block.splitlines():
                    if line.startswith("event: "):
                        name = line[7:].strip()
                    elif line.startswith("data: "):
                        payload = line[6:]
                if name:
                    events.append((name, payload))
except Exception as e:  # noqa: BLE001
    check("流式请求成功", False, str(e))

names = [n for n, _ in events]
check("事件顺序正确（meta → sources → token… → done）",
      names[:2] == ["meta", "sources"] and names[-1] == "done",
      f"实际顺序：{names[:6]}…{names[-1] if names else ''}")
check("确实逐字推送了（token 事件有多个）", names.count("token") > 3,
      f"token 事件只有 {names.count('token')} 个")

if events and events[-1][0] == "done":
    done = json.loads(events[-1][1])
    stage = done.get("stage_latency", {})
    check("首字延迟被记录下来了", "first_token" in stage, f"stage_latency = {stage}")
    if "first_token" in stage:
        print(f"         首字延迟 {stage['first_token']}ms，总耗时 {done.get('latency_ms')}ms")

# ---------- 3. 点赞点踩 ----------
print("\n[3] 点赞点踩")
status, body = call("POST", f"/messages/{assistant_id}/feedback", user_token, {"rating": 1})
check("点赞成功", status == 201, f"实际 {status}: {body.get('message')}")

status, body = call("POST", f"/messages/{assistant_id}/feedback", user_token, {"rating": -1, "comment": "改成点踩试试"})
check("改成点踩成功（覆盖而不是新增）", status == 201)

# 检查历史消息里带回了评价状态
status, body = call("GET", f"/conversations/{conversation_id}/messages", user_token)
messages = body.get("data", [])
assistant_msgs = [m for m in messages if m["role"] == "assistant"]
check("历史消息带回了我的评价状态",
      bool(assistant_msgs) and assistant_msgs[-1].get("my_feedback") == -1,
      f"my_feedback = {assistant_msgs[-1].get('my_feedback') if assistant_msgs else 'N/A'}")

status, _ = call("DELETE", f"/messages/{assistant_id}/feedback", user_token)
check("取消评价成功", status == 200)

# ---------- 4. 缓存命中（验证 cache_hit 不再恒为 False）----------
print("\n[4] 缓存命中")
status, body = call(
    "POST",
    "/chat/query",
    user_token,
    {"question": "电池容量是多少？", "kb_id": kb_id},
)
check("重复提问成功（应该命中缓存）", status == 200)

status, body = call("GET", "/admin/stats/overview", admin_token)
if status == 200:
    qa = body["data"]["qa"]
    check("缓存命中率不再是恒为 0（bug 已修）",
          qa["cache_hit_rate"] > 0,
          f"命中率 = {qa['cache_hit_rate']}%，命中 {qa['cache_hits']} 次")
    print(f"         问答总数 {qa['total']}，命中 {qa['cache_hits']} 次，"
          f"命中率 {qa['cache_hit_rate']}%")
    print(f"         平均响应延迟 {qa['avg_latency_ms']}ms（不含缓存命中）")
    print(f"         平均首字延迟 {qa['avg_first_token_ms']}ms")

# ---------- 5. 看板各接口 ----------
print("\n[5] 统计看板各接口")
status, body = call("GET", "/admin/stats/overview", admin_token)
check("总览接口正常", status == 200 and body.get("data") is not None)
if status == 200:
    d = body["data"]
    check("总览含用户/文档/问答/反馈四组数据",
          all(k in d for k in ("users", "documents", "qa", "feedback")))

status, body = call("GET", "/admin/stats/trend?days=7", admin_token)
check("趋势接口正常", status == 200)
if status == 200:
    items = body["data"]
    check("趋势返回 7 天且缺失日期补 0", len(items) == 7)
    print(f"         最后一天：{items[-1]}")

status, body = call("GET", "/admin/stats/top-questions?limit=5", admin_token)
check("高频问题接口正常", status == 200 and len(body["data"]) > 0)
if status == 200 and body["data"]:
    print(f"         最高频：{body['data'][0]}")

status, body = call("GET", "/admin/stats/recent-feedback?rating=-1", admin_token)
check("反馈列表接口正常", status == 200)

status, body = call("GET", "/admin/stats/recent-feedback?rating=5", admin_token)
check("非法的 rating 过滤值被拒（422）", status == 422, f"实际 {status}")

status, body = call("GET", "/admin/stats/runtime", admin_token)
check("运行时状态接口正常", status == 200)

status, body = call("GET", "/stats/me", user_token)
check("个人统计接口正常", status == 200)
if status == 200:
    print(f"         我的统计：{body['data']}")

# ---------- 6. 限流 ----------
print("\n[6] 全局限流（登录接口防暴力破解）")
hit_429 = False
for i in range(75):
    status, body = call("POST", "/auth/login", body={"username": "no_such_user", "password": "x"})
    if status == 429:
        hit_429 = True
        print(f"         第 {i + 1} 次请求被限流：{body.get('message')}")
        break
check("连续请求会被限流（429）", hit_429, "连发 75 次都没被限流")

print("\n" + "=" * 60)
print(f"通过 {passed}，失败 {failed}")
print("=" * 60)
sys.exit(0 if failed == 0 else 1)
