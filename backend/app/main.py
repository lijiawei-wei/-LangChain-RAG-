"""后端服务总入口。

职责：把各个零件组装起来（配置、数据库、路由、跨域、异常处理），
然后交给 uvicorn 启动 HTTP 服务。

启动命令（在 backend 目录下）：
    .venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.router import api_router
from app.core.config import settings
from app.core.middleware import RateLimitMiddleware
from app.db.init_db import init_db

# ---------- 控制台编码修正 ----------
# 中文版 Windows 的控制台默认编码是 GBK（cp936），Python 检测到后也跟着用 GBK 输出。
# 这会带来两个问题：
#   1. 日志里一旦出现 GBK 表示不了的字符（比如 emoji、某些生僻字或第三方库的
#      特殊符号），会直接抛 UnicodeEncodeError，把日志输出打断
#   2. 同一份代码在 Linux / Docker 里跑是 UTF-8，本地是 GBK，
#      日志文件在不同环境下编码不一致，排查问题时很难受
# 所以这里统一强制成 UTF-8。配合启动脚本里的 chcp 65001 效果最好。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 某些特殊环境下 stdout 不支持重设编码（比如被重定向到不支持的对象），
            # 这时保持原样即可，不影响服务启动
            pass

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """服务启动和关闭时要做的事。

    启动时：建数据目录、建表、确保管理员账号存在。
    关闭时：目前无需清理（SQLite 连接由连接池自动管理）。
    """
    logger.info("正在启动 %s ...", settings.APP_NAME)

    if settings.DEBUG and settings.JWT_SECRET_KEY == "dev-only-secret-change-me":
        logger.warning(
            "检测到正在使用默认的 JWT_SECRET_KEY，仅可用于本地开发！"
            "上线前请在 .env 中改成随机长字符串。"
        )

    init_db()
    logger.info("服务已就绪，接口文档：http://127.0.0.1:8000/docs")

    yield

    logger.info("服务已关闭")


app = FastAPI(
    title=settings.APP_NAME,
    description="基于 LangChain 的 RAG 企业级电商知识库问答系统",
    version="1.0.0",
    lifespan=lifespan,
    # 生产环境关闭接口文档，避免把内部接口结构暴露出去
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)


# ---------- 全局限流 ----------
# **注意这里的顺序，它决定了限流能不能正常工作**：
# Starlette 的 add_middleware 是「后添加的包在最外层」，
# 所以限流要写在跨域中间件**前面**，让跨域包在它外面。
#
# 如果写反了会出什么事：被限流的请求返回 429 时，这个响应不经过跨域中间件，
# 于是少了 Access-Control-Allow-Origin 这个响应头，浏览器会直接把整个响应丢掉。
# 前端拦截器拿不到内容，只能显示"网络错误"——
# 用户看到的是"打不开"，而不是"你操作太快了"，完全无从下手。
app.add_middleware(RateLimitMiddleware)


# ---------- 跨域 ----------
# 前端跑在 5173 端口、后端在 8000，浏览器默认不允许跨端口请求，
# 这里显式放行前端地址。注意不能用 allow_origins=["*"]，
# 那样虽然方便但会允许任意网站调用我们的接口。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # 让前端能读到这几个响应头（下载文件名、以及后面 SSE 可能用到的自定义头）
    expose_headers=["Content-Disposition"],
)


# ---------- 统一异常处理 ----------
# 目标：无论出什么错，前端拿到的都是同样的 {code, message, data} 结构，
# 前端拦截器就能统一弹错误提示，不用每个接口单独判断。
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """处理主动抛出的 HTTPException（如 401 未登录、403 无权限、404 不存在）。

    **注意 headers 必须原样带过去**。这里是自己重新构造响应的，
    如果不管 exc.headers，抛异常时设的那些响应头就全丢了：
      - 限流 429 的 Retry-After —— 客户端不知道要等多久
      - 未登录 401 的 WWW-Authenticate —— 按 HTTP 规范这是必须带的
    这类丢失非常隐蔽：状态码和错误消息都对，只有响应头没了，
    不专门去读 header 根本发现不了。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": str(exc.detail), "data": None},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """处理参数校验失败（前端传的数据格式不对）。

    默认返回的是一大坨嵌套错误对象，前端不好展示。这里提取成
    「字段名 + 原因」的人话提示，用户能直接看懂哪里填错了。
    """
    problems = []
    for err in exc.errors():
        # loc 形如 ("body", "password")，第一个元素是位置，后面才是字段名
        field = " → ".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
        problems.append(f"{field}：{err.get('msg', '格式不正确')}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "code": 422,
            "message": "参数校验失败 —— " + "；".join(problems),
            "data": None,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底：处理所有没被预料到的异常。

    关键点：完整的错误堆栈只写进服务端日志，返回给前端的是一句笼统的提示。
    如果直接把异常内容返回给前端，可能泄露数据库结构、文件路径等敏感信息。
    """
    logger.exception("未处理的异常：%s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"code": 500, "message": "服务器内部错误，请稍后重试", "data": None},
    )


# ---------- 路由 ----------
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["系统"], summary="健康检查")
async def health() -> dict:
    """健康检查接口。

    用途：确认服务活着、当前连的是哪个模型、跑在什么模式。
    部署后监控系统也会定时打这个接口来判断服务是否正常。
    """
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "debug": settings.DEBUG,
        "llm_model": settings.LLM_MODEL,
        "embedding_model": settings.EMBEDDING_MODEL,
        "rerank_model": settings.RERANK_MODEL,
    }
