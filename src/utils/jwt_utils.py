import uuid
from datetime import datetime, timedelta, timezone

import jwt

from src.core.config import get_settings
from loguru import logger

ALGORITHM = "HS256"

# access token 默认有效期（分钟）。登出后残留凭证的暴露窗口 ≈ 这个值，
# 因此必须有服务端撤销能力兜底（见 src/utils/token_revocation.py）。
DEFAULT_ACCESS_TOKEN_TTL_MINUTES = 30


def _secret_key() -> str:
    settings = get_settings()
    key = settings.JWT_SECRET_KEY
    if key:
        return key
    return "dev-only-insecure-secret"


def _access_ttl_seconds() -> int:
    settings = get_settings()
    minutes = int(getattr(settings, "ACCESS_TOKEN_TTL_MINUTES", DEFAULT_ACCESS_TOKEN_TTL_MINUTES))
    # 下限 1 分钟：避免误配成 0 导致刚签发就过期
    return max(60, minutes * 60)


# 配置OAuth2 Bearer 模式
from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# 可选鉴权：用于"允许匿名访问、登录则识别身份"的端点（如聊天）。
# auto_error=False：缺少 Token 时返回 None 而不是抛 401。
oauth2_scheme_optional = OAuth2PasswordBearer(
    # 告诉文档"想拿 token，去这个 URL 登录"。不会发真正的 HTTP 请求，纯文档元数据。你可以点 Swagger 页面右上角的 🔒 Authorize 按钮登录，就是靠这个字段。
    tokenUrl="/api/v1/auth/login", auto_error=False
)


# 编码JWT，创建一个令牌。登录成功后，把用户信息传递过来。 生成的令牌以后发给前端。
# 前端每次请求都需要携带自己的授权令牌。
#
# 除了业务字段，还会写入三个约定字段：
# - jti：令牌唯一 ID，登出时按它加入 Redis 撤销名单（无状态 JWT 无法"删除"）
# - iat：签发时间，用于"登出全部设备"时按时间窗批量失效
# - exp：过期时间，默认 30 分钟（配置 ACCESS_TOKEN_TTL_MINUTES）
def encode_jwt(payload: dict, ttl_seconds: int | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload_copy = payload.copy()
    payload_copy.setdefault("jti", uuid.uuid4().hex)
    payload_copy.setdefault("typ", "access")
    payload_copy["exp"] = now + timedelta(seconds=ttl_seconds if ttl_seconds is not None else _access_ttl_seconds())
    payload_copy["iat"] = now

    token = jwt.encode(payload_copy, key=_secret_key(), algorithm=ALGORITHM)
    return token


# 解码JWT，验证令牌的有效性。[从令牌中获取用户信息]
# 每次请求到达后端时，从请求头中提取令牌，然后调用此函数验证令牌的有效性。
# 返回 当时用户的信息（payload里面不要放敏感数据）
def verify_jwt(token: str) -> dict:
    try:
        payload = jwt.decode(token, key=_secret_key(), algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise Exception("token已经过期")
    except jwt.InvalidTokenError:
        raise Exception("非法token")
    except Exception as e:
        logger.warning(f"token校验失败: {type(e).__name__}")
        raise Exception("token校验失败")


def token_remaining_seconds(payload: dict) -> int:
    """token 剩余有效期（秒），用于设置撤销记录的 TTL；已过期返回 0。"""
    exp = payload.get("exp")
    if not exp:
        return 0
    try:
        remaining = int(exp) - int(datetime.now(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return 0
    return max(0, remaining)


def token_issued_at(payload: dict) -> float | None:
    """签发时间（unix 秒）。JWT 内部为整数秒，因此比较精度为秒级。"""
    iat = payload.get("iat")
    if iat is None:
        return None
    try:
        return float(iat)
    except (TypeError, ValueError):
        return None
