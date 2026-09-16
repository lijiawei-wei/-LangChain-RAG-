"""全局限流中间件。

**为什么需要它**（这是补上的一个真实漏洞）：

项目里本来就有 `SlidingWindowLimiter`，但只接在了问答接口上——
它被定义、被测试重置、被统计接口读取，却**从来没有中间件或依赖调用它**。
结果是登录、注册这些完全不需要身份就能调的接口没有任何频率限制，
攻击者可以拿一个字典无限次尝试密码，慢慢把账号试出来。

限流对登录接口的意义和处理高并发完全不同：它不是"保护服务器别被打挂"，
而是"让暴力破解在时间上不可行"——每分钟只允许试 60 次，
试完一个常见密码字典需要的时间就从几分钟变成几年。
"""

import json
import logging

from app.core.rate_limit import api_limiter
from app.core.security import decode_token

logger = logging.getLogger(__name__)

# 这些路径不做全局限流：
#   /api/v1/chat  —— 问答接口有**自己更严格的一套**（见 chat_service.check_rate_limit）。
#                    两边都限会导致同一个请求被扣两次配额，用户看到的提示还不一样，
#                    排查起来非常费劲。问答走它自己那套就够了。
#   /health       —— 监控探针会高频调用，限流会让监控误报服务不可用
#   /docs 等      —— 开发用的接口文档页面
EXEMPT_PREFIXES = (
    "/api/v1/chat",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _client_key(scope) -> str:
    """决定这个请求按什么来计数。

    **已登录的按用户算，没登录的按 IP 算。** 这个组合是有讲究的：

      - 按用户算：同一个办公室的多个用户走同一个出口 IP，
        如果一律按 IP 算，他们会被当成一个人共享配额，互相拖累
      - 按 IP 算：登录接口这时候还没有身份可依据，而它恰恰是最需要
        被限流的（防暴力破解），只能靠 IP

    注意这里只解析令牌、**不做完整校验**（不查库、不比对版本号）：
    中间件在每个请求上都要跑，能省一次数据库查询就省一次。
    解析失败一律当作未登录，按 IP 计数——就算有人伪造令牌想绕过限流，
    他也只是把自己降级到"按 IP 限"这一档，占不到便宜。
    """
    headers = scope.get("headers") or []
    for name, value in headers:
        if name == b"authorization":
            token = value.decode("latin-1").removeprefix("Bearer ").strip()
            payload = decode_token(token) if token else None
            if payload and payload.get("sub"):
                return f"user:{payload['sub']}"
            break

    # 没有身份就用 IP。client 可能为 None（比如测试客户端），兜底成 unknown
    client = scope.get("client")
    return f"ip:{client[0]}" if client else "ip:unknown"


def _too_many_requests_body(wait_seconds: int) -> bytes:
    """构造 429 的响应体。

    格式必须和 main.py 里那几个异常处理器一致（{code, message, data}），
    否则前端拦截器认不出来，只会显示"网络错误"，
    用户看到的就是一个没有任何解释的失败。
    """
    return json.dumps(
        {
            "code": 429,
            "message": f"操作太频繁了，请 {wait_seconds} 秒后再试",
            "data": None,
        },
        ensure_ascii=False,
    ).encode("utf-8")


class RateLimitMiddleware:
    """全局限流的 ASGI 中间件。

    **为什么手写 ASGI 中间件，而不是用 FastAPI 的 @app.middleware("http")**：

    后者底层是 Starlette 的 BaseHTTPMiddleware，它会把响应包一层、
    自己接管数据流的转发。对于普通的 JSON 接口没影响，但对 SSE 流式接口
    有把数据攒起来一起发的风险——一旦发生，打字机效果就没了，
    而这种问题从代码上完全看不出来（接口能通、内容也对，就是不"流"了）。

    这里虽然已经把问答接口排除在限流之外，但它仍然要**穿过**这个中间件。
    纯 ASGI 中间件除了判断和放行什么都不做，不碰数据流，
    从根上避免了这个问题。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        # 只处理 HTTP 请求。WebSocket 和 lifespan 事件直接放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # 浏览器的跨域预检请求（OPTIONS）不能计数：
        # 它不是用户的操作，每个真实请求前面都会先来一发，
        # 算进去等于把配额凭空砍掉一半
        if scope.get("method") == "OPTIONS" or path.startswith(EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        allowed, wait_seconds = api_limiter.check(_client_key(scope))
        if not allowed:
            logger.warning("请求被限流：%s %s", scope.get("method"), path)
            await self._reject(send, wait_seconds)
            return

        await self.app(scope, receive, send)

    async def _reject(self, send, wait_seconds: int) -> None:
        """直接发出 429 响应，不进入后面的业务处理。

        这里手工拼 ASGI 消息，是因为中间件层拿不到 FastAPI 的
        Request/Response 对象，只能按 ASGI 协议的格式自己发。
        """
        body = _too_many_requests_body(wait_seconds)
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    # 告诉客户端多久之后可以重试，规范里定义的响应头
                    (b"retry-after", str(wait_seconds).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
