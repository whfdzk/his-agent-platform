# ============================================================
# 后台管理员账号 Model 定义
#
# 与患者账号（patients）完全隔离：管理端的操作对象是平台级配置（模型、
# 知识库、统计），一旦被患者 token 打通就是越权。因此单独一张表、单独签发
# 带 role=admin 的 JWT，患者 token 在管理端接口一律 403。
# ============================================================

from sqlalchemy import String, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel

# 角色：本期只有 admin，字段留出来是为了后续细分为 viewer/operator
ROLE_ADMIN = "admin"


class Admin(BaseModel):
    __tablename__ = "admins"

    username: Mapped[str] = mapped_column(String(64), nullable=False, comment="登录账号")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码哈希")
    display_name: Mapped[str] = mapped_column(String(100), default="", comment="展示名称")
    role: Mapped[str] = mapped_column(
        String(32), default=ROLE_ADMIN, comment="角色：admin（预留 viewer/operator）"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")

    __table_args__ = (
        # 登录账号全局唯一：应用层校验之外再加 DB 约束，避免并发建号撞号
        Index("ix_admins_username", "username", unique=True),
    )
