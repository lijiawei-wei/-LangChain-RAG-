"""问答接口。

提供两个版本：
  POST /chat/query   非流式，一次返回完整回答（接口测试和效果评估脚本用）
  POST /chat/stream  流式，用 SSE 逐块推送（浏览器实际走这个）
"""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentUser, DbSession
from app.schemas.chat import ChatRequest, ChatResponse
from app.schemas.common import ApiResponse
from app.services import chat_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["问答"])


@router.post("/query", response_model=ApiResponse[ChatResponse], summary="知识库问答（非流式）")
async def query(
    payload: ChatRequest,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[ChatResponse]:
    """提问并获取完整回答。

    两种用法：
      - 传 `conversation_id`：在已有会话里继续追问，会自动带上历史对话
      - 不传：自动开一个新会话。可配合 `kb_id` 指定只检索某个知识库

    回答里带引用来源列表，每条标注了它在回答中对应的编号 [n]，
    以及这条来源是否真的被用到了（used 字段）。
    """
    try:
        result = await chat_service.ask(db, user, payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("问答处理失败")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"生成回答时出错了，请稍后重试。（{type(e).__name__}）",
        ) from e

    return ApiResponse.ok(result)


# ============================================================
# 流式问答
# ============================================================
def _sse(event: str, data: dict) -> str:
    """把一条事件格式化成 SSE 协议要求的文本。

    SSE 的格式是固定的：
        event: 事件名
        data: 数据
        （空行表示这条消息结束）

    数据用 JSON 编码有个额外好处：JSON 会把换行转义成 \\n，
    所以回答里的换行不会破坏 SSE 的「一行一条数据」结构。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.post("/stream", summary="知识库问答（流式，SSE）")
async def stream(
    payload: ChatRequest,
    db: DbSession,
    user: CurrentUser,
    request: Request,
) -> StreamingResponse:
    """流式问答，用 SSE 逐块推送。

    事件顺序：
        meta     —— 会话信息（conversation_id、用户消息 ID）
        sources  —— 引用来源列表（先于文字推送，前端可提前渲染右侧面板）
        token    —— 增量文本，会连续推送很多次
        done     —— 结束，带最终统计和完整引用
        error    —— 出错了

    为什么前端不能用 EventSource：
    EventSource 只支持 GET 请求，也不能自定义请求头，
    而我们的接口是 POST 且需要带 Authorization 令牌。
    所以前端用 fetch + ReadableStream 自己解析，见 utils/sse.js。
    """
    cancel_event = asyncio.Event()

    # **校验必须在创建 StreamingResponse 之前做完**。
    # 一旦开始返回流，HTTP 状态码就已经发出去了（200），
    # 之后再出什么问题都只能塞进事件里，前端拿不到真正的错误码。
    # 所以限流、会话归属、知识库是否存在这些检查都放在这里，
    # 让它们能以正常的 429 / 404 返回，和非流式接口保持一致。
    prepared = await chat_service.prepare_stream(db, user, payload)

    async def event_generator():
        try:
            async for event_name, data in chat_service.ask_stream(
                db, user, payload, prepared, cancel_event
            ):
                yield _sse(event_name, data)

        except asyncio.CancelledError:
            # 用户关掉了页面或点了停止。通知生成环节提前收尾，
            # 免得白白跑完一次完整生成再把结果丢掉（那是要花钱的）
            cancel_event.set()
            logger.info("客户端断开连接，已通知停止生成")
            raise

        except HTTPException as e:
            # 走到这里说明是**开始推流之后**才发现的业务错误，
            # 状态码已经发出去了改不了，只能转成 error 事件。
            # 但至少要把真实原因告诉用户——如果这里也统一成
            # "生成回答时出错了"，用户看到的就是一句毫无信息量的话
            logger.warning("流式问答中断：%s", e.detail)
            yield _sse(
                "error",
                {
                    "code": e.status_code,
                    "message": str(e.detail),
                    "retry_after": (e.headers or {}).get("Retry-After"),
                },
            )

        except Exception as e:
            # 关键：**异常必须在生成器内部消化掉**，转成一条 error 事件推给前端。
            # 如果任由异常抛出，连接会直接断掉，前端只能看到一个"网络错误"，
            # 既不知道发生了什么，也可能出现界面永远转圈的情况。
            logger.exception("流式问答出错")
            yield _sse(
                "error",
                {
                    "message": f"生成回答时出错了：{type(e).__name__}",
                    "hint": "请稍后重试；如果持续失败，请检查模型服务是否正常",
                },
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # 禁止任何中间层缓存 SSE 响应
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # 这行是给 nginx 看的：告诉它不要缓冲这个响应。
            # 不设的话，nginx 会攒够一块再发，用户就看到"憋一下全出来"，
            # 流式效果完全失效。开发环境没有 nginx 影响不大，
            # 但将来部署到服务器上这行是必须的
            "X-Accel-Buffering": "no",
        },
    )
