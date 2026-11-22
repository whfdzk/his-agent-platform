# src/modules/clinical/model.py
#
# 临床动作（高危操作）的草稿与确认记录。
#
# 为什么需要这张表：
#   挂号这类动作一旦落库就是既成事实，患者反悔只能再走取消。此前"是否要预约"的
#   待确认状态只存在 Redis 里（ex=600），既没有审计留痕（谁在什么时候提议了什么、
#   患者怎么回应），也没有任何状态可供管理端/医护端查看，Redis 一重启就整条丢失。
#
# 状态机（只允许这些迁移，服务层强制校验）：
#
#   draft ──► pending_confirm ──► approved   （执行成功，result_ref 落预约号等）
#                     │
#                     ├──► rejected          （患者/医护明确拒绝）
#                     └──► expired           （超过 expires_at，惰性判定，不需要扫描任务）
#
# 设计要点：
# - payload 保存"拟执行动作的完整参数"，确认时直接按参数执行，不再重新问模型 —— 否则
#   患者点完确认还要等一次 LLM 生成，体验上等于没确认成功。
# - approved 之后再 approve 必须幂等返回，不能重复建预约（靠 status + 行级校验）。
# - 不在此表存 PHI：summary 只放科室/时间这类非敏感描述，payload 里若含患者信息由
#   调用方先脱敏。

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin

# ── 动作类型 ────────────────────────────────────────────────────────────
ACTION_APPOINTMENT = "appointment"   # 挂号预约
ACTION_HANDOFF = "handoff"           # 问诊结论移交（含预约意向）
ACTION_MEDICATION = "medication"     # 用药建议（B2 后续）
ACTION_ORDER = "order"               # 检查/检验开单（B2 后续）

# ── 状态 ────────────────────────────────────────────────────────────────
STATUS_DRAFT = "draft"
STATUS_PENDING = "pending_confirm"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"

# 只有处于这个状态的记录才能被确认/拒绝
OPEN_STATUSES = (STATUS_DRAFT, STATUS_PENDING)
TERMINAL_STATUSES = (STATUS_APPROVED, STATUS_REJECTED, STATUS_EXPIRED)

# 谁发起的
SOURCE_AGENT = "agent"       # 问诊链路自动生成
SOURCE_TOOL = "tool"         # 工具调用生成
SOURCE_CLINICIAN = "clinician"  # 医护发起


class ClinicalAction(BaseModel, TenantMixin):
    __tablename__ = "clinical_actions"

    action_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="动作类型：appointment / handoff / medication / order"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=STATUS_DRAFT,
        comment="状态：draft / pending_confirm / approved / rejected / expired",
    )

    patient_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="患者 ID（游客或未识别时为空）"
    )
    user_id: Mapped[str] = mapped_column(String(100), default="", comment="发起会话的用户标识")
    session_id: Mapped[str] = mapped_column(String(100), default="", comment="会话 ID")
    thread_id: Mapped[str] = mapped_column(String(160), default="", comment="LangGraph thread_id")
    trace_id: Mapped[str] = mapped_column(String(64), default="", comment="关联 trace，用于回溯")

    source: Mapped[str] = mapped_column(
        String(24), default=SOURCE_AGENT, comment="发起来源：agent / tool / clinician"
    )
    requested_by: Mapped[str] = mapped_column(
        String(64), default="", comment="发起者标识，如 agent:consultation / tool:appointment_manager"
    )

    summary: Mapped[str] = mapped_column(
        String(255), default="", comment="给患者看的一句话描述（不含 PHI）"
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, comment="拟执行动作的完整参数")

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="待确认超时时间，过期后惰性置 expired"
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认/拒绝时间"
    )
    decided_by: Mapped[str] = mapped_column(
        String(64), default="", comment="决策人：patient:<id> / clinician:<id> / system"
    )
    reject_reason: Mapped[str] = mapped_column(String(255), default="", comment="拒绝原因")

    result_ref: Mapped[str] = mapped_column(
        String(64), default="", comment="执行结果引用，如预约号 APTXXXX"
    )
    note: Mapped[str] = mapped_column(Text, default="", comment="备注")

    __table_args__ = (
        Index("ix_clinical_actions_patient", "patient_id"),
        Index("ix_clinical_actions_status", "status"),
        Index("ix_clinical_actions_session", "session_id"),
        # 待确认清单的查询主路径：按状态 + 过期时间过滤
        Index("ix_clinical_actions_status_expires", "status", "expires_at"),
        Index("ix_clinical_actions_tenant", "tenant_id"),
    )


# ── 临床事件（B3） ───────────────────────────────────────────────────────
#
# 为什么需要这张表：
#   急症识别此前的产出只有一句回复文案（"请立即前往急诊/120"），落库为零，
#   因此无法回答"这个平台识别过多少红旗症状、分级是否合理、哪些会话最后真去了急诊"。
#   把分级结果作为事件行落库，才能支撑后续的医护看板、质控抽查与模型调优。
#
# 设计要点：
# - 只落 level 非 green 的事件（green 等于"没识别出风险"，每个会话一条就是纯噪声）；
# - 不存原始用户描述：原文里带 PHI，且 chat_messages 已按脱敏规则保存对话，
#   这里只留分级、原因与症状词；
# - level 用字符串而非枚举，后续加档（如 black）不必迁移类型。

EVENT_EMERGENCY = "emergency_triage"  # 急症分级事件

LEVEL_RED = "red"        # 立即危及生命：立即急诊 / 拨打 120
LEVEL_ORANGE = "orange"  # 潜在危及生命：短时间内急诊评估
LEVEL_YELLOW = "yellow"  # 需尽快就医：今日/次日门诊或急诊
LEVEL_GREEN = "green"    # 未见红旗症状：常规问诊

LEVELS = (LEVEL_RED, LEVEL_ORANGE, LEVEL_YELLOW, LEVEL_GREEN)
# 数值越大越紧急，便于排序与阈值判断
LEVEL_SEVERITY = {LEVEL_GREEN: 0, LEVEL_YELLOW: 1, LEVEL_ORANGE: 2, LEVEL_RED: 3}


class ClinicalEvent(BaseModel, TenantMixin):
    __tablename__ = "clinical_events"

    event_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EVENT_EMERGENCY,
        comment="事件类型：emergency_triage 等",
    )
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=LEVEL_GREEN,
        comment="分级：red / orange / yellow / green",
    )
    reason: Mapped[str] = mapped_column(
        String(255), default="", comment="判定原因（模型给出的短语，不含患者原话）"
    )
    symptoms: Mapped[list] = mapped_column(
        JSONB, default=list, comment="触发的症状词列表（已去重）"
    )

    patient_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="患者 ID（游客为空）"
    )
    user_id: Mapped[str] = mapped_column(String(100), default="", comment="会话用户标识")
    session_id: Mapped[str] = mapped_column(String(100), default="", comment="会话 ID")
    thread_id: Mapped[str] = mapped_column(String(160), default="", comment="LangGraph thread_id")
    trace_id: Mapped[str] = mapped_column(String(64), default="", comment="关联 trace")

    source: Mapped[str] = mapped_column(
        String(24), default=SOURCE_AGENT, comment="来源：agent / tool / clinician"
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, default=dict, comment="附加信息（如模型原文命中、轮次等）"
    )

    __table_args__ = (
        Index("ix_clinical_events_patient", "patient_id"),
        Index("ix_clinical_events_level", "level"),
        Index("ix_clinical_events_session", "session_id"),
        Index("ix_clinical_events_type_level", "event_type", "level"),
        Index("ix_clinical_events_tenant", "tenant_id"),
    )
