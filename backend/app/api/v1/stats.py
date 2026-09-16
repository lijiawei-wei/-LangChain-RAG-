"""统计接口。

分两组，权限完全不同，所以放在一个文件里但用两个 router：

  - `/stats/me`       —— 任何登录用户都能看**自己的**统计（个人中心用）
  - `/admin/stats/*`  —— 仅管理员，看**全站**统计（统计看板用）

为什么要分开而不是合成一组：全站统计包含所有用户的问答量、token 消耗、
高频问题，这些属于运营数据，普通用户不该看到别人问了什么。
个人统计里 user_id 是写死的（取当前登录用户），
不存在"传别人的 ID 去看别人数据"的可能。
"""

from fastapi import APIRouter, HTTPException, Query, status

from app.core.deps import AdminUser, CurrentUser, DbSession
from app.schemas.common import ApiResponse
from app.schemas.stats import (
    FeedbackDetailOut,
    MyStats,
    RuntimeStats,
    TopQuestion,
    TrendItem,
)
from app.services import stats_service

# 普通用户可见的部分
router = APIRouter(prefix="/stats", tags=["统计"])

# 管理员专属部分。前缀里带 admin，一眼能看出权限级别
admin_router = APIRouter(prefix="/admin/stats", tags=["统计（管理员）"])


# ============================================================
# 个人统计
# ============================================================
@router.get("/me", response_model=ApiResponse[MyStats], summary="我的问答统计")
def my_stats(db: DbSession, user: CurrentUser) -> ApiResponse[MyStats]:
    """当前用户的问答统计，个人中心页面用。"""
    return ApiResponse.ok(MyStats(**stats_service.get_my_stats(db, user.id)))


# ============================================================
# 全站总览
# ============================================================
@admin_router.get("/overview", response_model=ApiResponse[dict], summary="总览指标")
def overview(db: DbSession, _: AdminUser) -> ApiResponse[dict]:
    """看板顶部那排数字卡片。

    包含用户数、文档数、问答量、缓存命中率、平均延迟、平均首字延迟、
    token 消耗、点赞点踩数。
    """
    return ApiResponse.ok(stats_service.get_overview(db))


@admin_router.get("/runtime", response_model=ApiResponse[RuntimeStats], summary="运行时状态")
def runtime(_: AdminUser) -> ApiResponse[RuntimeStats]:
    """进程内的实时状态：缓存占用了多少、正在生成几个问答。

    **重启服务会归零**，它反映的是"此刻"而不是"累计"。
    """
    return ApiResponse.ok(RuntimeStats(**stats_service.get_runtime_stats()))


@admin_router.get("/trend", response_model=ApiResponse[list[TrendItem]], summary="问答趋势")
def trend(
    db: DbSession,
    _: AdminUser,
    days: int = Query(7, ge=1, le=90, description="往回看多少天"),
) -> ApiResponse[list[TrendItem]]:
    """最近 N 天每天的问答情况。

    没有问答的那天也会返回（各项为 0），前端画折线图时不会断。
    日期按**东八区**算，不是 UTC——否则早上 8 点前的提问会被算到前一天。
    """
    items = stats_service.get_trend(db, days)
    return ApiResponse.ok([TrendItem(**item) for item in items])


@admin_router.get(
    "/top-questions", response_model=ApiResponse[list[TopQuestion]], summary="高频问题排行"
)
def top_questions(
    db: DbSession,
    _: AdminUser,
    limit: int = Query(10, ge=1, le=50),
) -> ApiResponse[list[TopQuestion]]:
    """问得最多的问题。

    句尾标点会被归一化，所以「电池容量是多少」和「电池容量是多少？」算同一个问题。
    """
    items = stats_service.get_top_questions(db, limit)
    return ApiResponse.ok([TopQuestion(**item) for item in items])


@admin_router.get(
    "/recent-feedback",
    response_model=ApiResponse[list[FeedbackDetailOut]],
    summary="最近的反馈记录",
)
def recent_feedback(
    db: DbSession,
    _: AdminUser,
    rating: int | None = Query(
        None, description="只看点赞传 1，只看点踩传 -1，不传则全部都看"
    ),
    limit: int = Query(20, ge=1, le=100),
) -> ApiResponse[list[FeedbackDetailOut]]:
    """最近的点赞/点踩记录，带问题和回答的内容。

    **这是发现知识库盲区的主要入口**：查到哪类问题被集中点踩，
    就知道该补哪方面的商品资料了。
    """
    # 这里手工校验而不用 Literal[1, -1]，是有原因的：
    # 查询参数从 URL 里取出来永远是**字符串**（"-1"），
    # 而 Literal[1, -1] 里是整数，Pydantic 不会把字符串转成整数再比对，
    # 结果就是传什么都报 422、包括合法的 -1。
    # （请求体里没有这个问题——JSON 里的数字本来就是数字类型。）
    if rating is not None and rating not in (1, -1):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="rating 只能是 1（点赞）或 -1（点踩）",
        )

    items = stats_service.get_recent_feedback(db, rating, limit)
    return ApiResponse.ok([FeedbackDetailOut(**item) for item in items])
