"""答案反馈接口（点赞/点踩）。

权限设计：**任何登录用户都能评价，但只能评价自己的回答**。
归属校验统一在 feedback_service._get_rateable_message 里做，
越权的人拿到的是 404（而不是 403），不会泄露"这条消息存在"。
"""

from fastapi import APIRouter, status

from app.core.deps import CurrentUser, DbSession
from app.schemas.common import ApiResponse
from app.schemas.feedback import FeedbackCreate, FeedbackInfo
from app.services import feedback_service

router = APIRouter(prefix="/messages", tags=["答案反馈"])


@router.post(
    "/{message_id}/feedback",
    response_model=ApiResponse[FeedbackInfo],
    status_code=status.HTTP_201_CREATED,
    summary="给回答点赞或点踩",
)
def submit_feedback(
    message_id: int,
    payload: FeedbackCreate,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[FeedbackInfo]:
    """对某条助手回答表态。rating 传 1 表示点赞，-1 表示点踩。

    重复提交同一个 message 是**覆盖**而不是新增，详见 feedback_service 的说明。
    """
    feedback = feedback_service.submit_feedback(db, message_id, user.id, payload)
    return ApiResponse.ok(FeedbackInfo.model_validate(feedback), message="感谢反馈")


@router.delete(
    "/{message_id}/feedback",
    response_model=ApiResponse[None],
    summary="取消对回答的评价",
)
def cancel_feedback(
    message_id: int, db: DbSession, user: CurrentUser
) -> ApiResponse[None]:
    """取消自己之前给这条回答的评价。

    前端在用户点击"已选中的那个按钮"时调用（再点一次赞 = 取消赞）。
    """
    feedback_service.cancel_feedback(db, message_id, user.id)
    return ApiResponse.ok(message="已取消评价")
