# src/modules/ops/admin_audit_model.py
#
# 管理端操作审计表（C4 续 · 第 ③ 件）。
#
# 背景：医生档案 PUT /api/v1/admin/clinicians/{id}/profile 此前只写 loguru 日志，
# 日志轮转/容器重建后"谁把某位医生改成停诊、从什么职称改成什么职称"无法回溯。
# 档案直接决定"能不能被挂号"，改错一个 status 就会让患者整周挂不上号，
# 必须留可查证的库内痕迹。
#
# 与 knowledge_audit_logs 的区别：
#   那张表记的是"患者问了什么、有没有文档支撑"，属于检索链路；
#   这张表记的是"管理员改了什么"，属于授权/配置变更，两者不合并。

from __future__ import annotations

from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin


class AdminAuditLog(BaseModel, TenantMixin):
    """一条管理端写操作记录：谁、对谁、改前改后、结果。"""
    __tablename__ = "admin_audit_logs"

    actor: Mapped[str] = mapped_column(String(100), default="", comment="操作人（admin JWT sub）")
    actor_name: Mapped[str] = mapped_column(String(100), default="", comment="操作人用户名")
    action: Mapped[str] = mapped_column(
        String(64), default="", comment="动作，如 clinician.profile.update"
    )
    target_kind: Mapped[str] = mapped_column(
        String(32), default="", comment="对象类型：clinician / patient / department"
    )
    target_id: Mapped[str] = mapped_column(String(64), default="", comment="对象 ID")
    before: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="变更前快照（只含本表字段，不含手机号等敏感信息）"
    )
    after: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="变更后快照")
    changed: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="实际发生变化的字段名列表"
    )
    result: Mapped[str] = mapped_column(
        String(16), default="ok", comment="结果：ok / rejected / failed"
    )
    note: Mapped[str] = mapped_column(Text, default="", comment="备注 / 拒绝原因（截断 255 字）")
    ip: Mapped[str] = mapped_column(String(64), default="", comment="来源 IP")
    request_id: Mapped[str] = mapped_column(String(64), default="", comment="请求 ID")

    __table_args__ = (
        Index("ix_admin_audit_actor", "actor"),
        Index("ix_admin_audit_action", "action"),
        Index("ix_admin_audit_target", "target_kind", "target_id"),
        Index("ix_admin_audit_logs_tenant", "tenant_id"),
    )
