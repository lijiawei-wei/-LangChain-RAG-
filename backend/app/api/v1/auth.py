"""认证接口。

把 auth_service 里的业务逻辑挂成 6 个网址，前端通过 HTTP 调这些网址。
这一层只负责「收参数 → 调服务 → 返回结果」，不写业务逻辑，
这样业务逻辑能被复用（比如命令行脚本也能调 auth_service）。
"""

from fastapi import APIRouter, HTTPException, status

from app.core.deps import CurrentUser, DbSession
from app.core.security import decode_token
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserInfo,
)
from app.schemas.common import ApiResponse
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post(
    "/register",
    response_model=ApiResponse[UserInfo],
    status_code=status.HTTP_201_CREATED,
    summary="注册新用户",
)
def register(payload: RegisterRequest, db: DbSession) -> ApiResponse[UserInfo]:
    """注册一个普通用户。

    注意：注册出来的账号永远是普通用户。管理员只能由系统启动时的种子数据创建，
    注册接口不提供指定角色的参数——否则任何人都能把自己注册成管理员。
    """
    user = auth_service.register_user(db, payload.username, payload.password)
    return ApiResponse.ok(UserInfo.model_validate(user), message="注册成功")


@router.post("/login", response_model=ApiResponse[TokenResponse], summary="登录")
def login(payload: LoginRequest, db: DbSession) -> ApiResponse[TokenResponse]:
    """校验用户名密码，成功后下发一对令牌。

    返回两个令牌，分工不同：
      access_token  —— 有效期短（默认 1 小时），访问业务接口时带着它
      refresh_token —— 有效期长（默认 7 天），access 过期后用它换新的
    这样即使 access 泄露，危害也被限制在 1 小时内。
    """
    user = auth_service.authenticate(db, payload.username, payload.password)
    return ApiResponse.ok(auth_service.build_token_response(user), message="登录成功")


@router.post("/refresh", response_model=ApiResponse[TokenResponse], summary="刷新访问令牌")
def refresh(payload: RefreshRequest, db: DbSession) -> ApiResponse[TokenResponse]:
    """用刷新令牌换一张新的访问令牌。

    什么时候会走到这里：用户正在用着系统，access_token 到期了。
    前端拦截器发现 401 就自动调这个接口换新的，然后重放刚才失败的请求，
    用户完全无感，不用重新登录。

    校验要点与访问业务接口一致，多一条：必须确认传进来的确实是刷新令牌，
    防止有人拿访问令牌来这里换新令牌（那样就变相延长了访问令牌寿命）。
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="刷新令牌无效或已过期，请重新登录",
    )

    claims = decode_token(payload.refresh_token)
    if claims is None or claims.get("type") != "refresh":
        raise invalid

    try:
        user_id = int(claims.get("sub", ""))
    except (TypeError, ValueError):
        raise invalid from None

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise invalid

    # 令牌版本号比对：用户改过密码的话，此前签发的刷新令牌在这里失效
    if claims.get("ver") != user.token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="密码已变更，请重新登录",
        )

    return ApiResponse.ok(auth_service.build_token_response(user), message="刷新成功")


@router.post("/logout", response_model=ApiResponse[None], summary="登出")
def logout(user: CurrentUser) -> ApiResponse[None]:
    """登出。

    为什么这里几乎什么都不做：JWT 是「无状态」的，令牌自带签名和有效期，
    服务端并没有保存一份「已登录名单」，所以没法单方面作废某一张令牌。

    真正的登出动作发生在前端：把本地存的令牌删掉即可。
    这个接口存在的意义是给前端一个明确的语义化调用点；将来若要加
    「登出时踢掉所有设备」的能力，只要在这里把 user.token_version += 1 就行。
    """
    return ApiResponse.ok(message="已登出")


@router.get("/me", response_model=ApiResponse[UserInfo], summary="获取当前用户信息")
def me(user: CurrentUser) -> ApiResponse[UserInfo]:
    """返回当前登录用户的信息。

    前端刷新页面后用它确认「我还登录着吗」，顺便拿到最新的角色信息
    （角色可能被管理员改过，不能只信登录时存在浏览器里的那一份）。
    """
    return ApiResponse.ok(UserInfo.model_validate(user))


@router.put("/password", response_model=ApiResponse[TokenResponse], summary="修改密码")
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    db: DbSession,
) -> ApiResponse[TokenResponse]:
    """修改密码。

    改完之后：
      - 该用户此前签发的所有令牌（含其它设备上的）立即失效，
        因为它们令牌里的版本号和用户的最新版本号对不上了
      - 但当前这台设备会拿到一套新令牌，所以不会被踢出去，体验更顺
    """
    auth_service.change_password(db, user, payload.old_password, payload.new_password)
    db.refresh(user)
    return ApiResponse.ok(
        auth_service.build_token_response(user),
        message="密码修改成功，其它设备需重新登录",
    )
