# ============================================================
# 平台管理域 Model 定义（admin 后台专用）
#
# - Agent          ：Agent 元数据（创建/编辑/发布），运行时通过 code 关联 src/agents 下的实现
# - Prompt         ：提示词定义；PromptVersion：版本化内容，单条激活版本
# - SystemConfig   ：系统参数 / 开关 / 字典，key 全局唯一
# - MembershipPlan / MembershipOrder / UserMembership：P2 会员域（套餐 / 订单 / 订阅）
#
# 与患者域（src/modules/medical）、运维域（src/modules/ops）边界：
# 本文件只放"管理端可操作的平台级配置"，不承载运行时埋点数据。
# ============================================================

from sqlalchemy import Boolean, Index, Integer, String, Text, BigInteger
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin

# Agent 状态：draft=草稿 / published=已发布 / disabled=已停用
AGENT_STATUS_DRAFT = "draft"
AGENT_STATUS_PUBLISHED = "published"
AGENT_STATUS_DISABLED = "disabled"

# 系统配置类型：param=参数 / switch=开关 / dict=字典
CONFIG_TYPE_PARAM = "param"
CONFIG_TYPE_SWITCH = "switch"
CONFIG_TYPE_DICT = "dict"


class Agent(BaseModel):
    """Agent 元数据。code 与 src/agents 下的 worker/supervisor 实现约定对应。"""
    __tablename__ = "agents"

    code: Mapped[str] = mapped_column(String(64), nullable=False, comment="唯一编码，如 inquiry/drug/knowledge")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="展示名称")
    agent_type: Mapped[str] = mapped_column(
        String(20), default="worker", comment="类型：supervisor/worker"
    )
    description: Mapped[str] = mapped_column(String(500), default="", comment="职责描述")
    avatar: Mapped[str] = mapped_column(String(200), default="", comment="图标/头像标识")
    status: Mapped[str] = mapped_column(
        String(20), default=AGENT_STATUS_DRAFT, comment="draft/published/disabled"
    )
    config: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="扩展配置（模型覆盖、工具开关等）"
    )
    published_at: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="最近发布时间（ISO 字符串，避免时区歧义）"
    )

    __table_args__ = (
        Index("uq_agents_code", "code", unique=True),
        Index("ix_agents_status", "status"),
    )


class Prompt(BaseModel):
    """提示词定义：一条 Prompt 对应多版本内容。"""
    __tablename__ = "prompts"

    key: Mapped[str] = mapped_column(String(64), nullable=False, comment="业务键，如 supervisor.system")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="展示名称")
    scene: Mapped[str] = mapped_column(
        String(50), default="agent", comment="使用场景：agent/tool/audit/other"
    )
    description: Mapped[str] = mapped_column(String(500), default="", comment="用途说明")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    active_version: Mapped[int] = mapped_column(
        Integer, default=0, comment="当前激活的版本号（0=尚未激活）"
    )

    __table_args__ = (
        Index("uq_prompts_key", "key", unique=True),
    )


class PromptVersion(BaseModel):
    """提示词版本：不可变内容快照，激活/回滚只改 prompts.active_version。"""
    __tablename__ = "prompt_versions"

    prompt_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="所属 Prompt ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号，从 1 递增")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="提示词正文")
    change_note: Mapped[str] = mapped_column(String(300), default="", comment="变更说明")
    created_by: Mapped[str] = mapped_column(String(64), default="", comment="创建人（管理员用户名）")

    __table_args__ = (
        Index("uq_prompt_versions_prompt_version", "prompt_id", "version", unique=True),
    )


class Skill(BaseModel):
    """技能（SOP）：一条技能对应多版正文。

    渐进加载：目录只下发 name + description（一句话），模型判断相关后再
    调 load_skill(key) 取正文，避免把几十条 SOP 全塞进上下文。
    发布/回滚只改 active_version，正文快照永不覆盖。
    """
    __tablename__ = "skills"

    key: Mapped[str] = mapped_column(String(64), nullable=False, comment="技能键，如 inquiry")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="展示名称")
    description: Mapped[str] = mapped_column(
        String(500), default="", comment="一句话简介（目录层，模型先看这个）"
    )
    tool_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="该技能声明的工具名（仅用于展示与校验，不直接放权）"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    active_version: Mapped[int] = mapped_column(
        Integer, default=0, comment="当前生效版本号（0=尚未发布，运行时不加载）"
    )
    mount_hint: Mapped[str] = mapped_column(
        String(200), default="", comment="挂载说明（人工备注，如：挂在问诊 Agent）"
    )

    __table_args__ = (
        Index("uq_skills_key", "key", unique=True),
    )


class SkillVersion(BaseModel):
    """技能正文快照：不可变，发布 / 回滚只改 skills.active_version。"""
    __tablename__ = "skill_versions"

    skill_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="所属技能 ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号，从 1 递增")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="SKILL.md 正文")
    change_note: Mapped[str] = mapped_column(String(300), default="", comment="变更说明")
    created_by: Mapped[str] = mapped_column(String(64), default="", comment="创建人（管理员用户名）")
    published_at: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="发布时间（ISO 字符串，未发布为空）"
    )

    __table_args__ = (
        Index("uq_skill_versions_skill_version", "skill_id", "version", unique=True),
    )


class SystemConfig(BaseModel):
    """系统配置：参数 / 开关 / 字典统一一张表，前端按 type 渲染不同控件。"""
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(100), nullable=False, comment="配置键，如 chat.max_turns")
    value: Mapped[str] = mapped_column(Text, default="", comment="配置值（字典存 JSON 数组）")
    config_type: Mapped[str] = mapped_column(
        String(20), default=CONFIG_TYPE_PARAM, comment="param/switch/dict"
    )
    group: Mapped[str] = mapped_column(String(50), default="general", comment="分组：general/chat/safety/member...")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="展示名称")
    description: Mapped[str] = mapped_column(String(500), default="", comment="说明")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否生效")

    __table_args__ = (
        Index("uq_system_configs_key", "key", unique=True),
        Index("ix_system_configs_group", "group"),
    )


# ── P2 会员域 ────────────────────────────────────────────────────────

class MembershipPlan(BaseModel, TenantMixin):
    """会员套餐定义。"""
    __tablename__ = "membership_plans"

    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="套餐名称，如 月度会员")
    price_cents: Mapped[int] = mapped_column(Integer, default=0, comment="价格（分）")
    duration_days: Mapped[int] = mapped_column(Integer, default=30, comment="有效期（天）")
    benefits: Mapped[str] = mapped_column(Text, default="", comment="权益说明，JSON 数组字符串")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否上架")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="展示排序，数字越小越靠前")

    __table_args__ = (
        Index("ix_membership_plans_enabled", "enabled"),
        Index("ix_membership_plans_tenant", "tenant_id"),
    )


class MembershipOrder(BaseModel, TenantMixin):
    """会员购买订单。本期人工/对接支付前的记录层。"""
    __tablename__ = "membership_orders"

    order_no: Mapped[str] = mapped_column(String(32), nullable=False, comment="订单号")
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="购买用户 ID（patients.id）")
    plan_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="套餐 ID")
    plan_name: Mapped[str] = mapped_column(String(100), default="", comment="下单时套餐名称快照")
    amount_cents: Mapped[int] = mapped_column(Integer, default=0, comment="实付金额（分）")
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="pending/paid/cancelled/refunded"
    )

    __table_args__ = (
        Index("uq_membership_orders_no", "order_no", unique=True),
        Index("ix_membership_orders_user", "user_id"),
        Index("ix_membership_orders_status", "status"),
        Index("ix_membership_orders_tenant", "tenant_id"),
    )


class UserMembership(BaseModel, TenantMixin):
    """用户当前会员订阅：一个用户一条有效记录，续费顺延 expire_at。"""
    __tablename__ = "user_memberships"

    user_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="用户 ID（patients.id）")
    plan_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="套餐 ID")
    plan_name: Mapped[str] = mapped_column(String(100), default="", comment="套餐名称快照")
    start_at: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="开始时间 ISO")
    expire_at: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="到期时间 ISO")
    status: Mapped[str] = mapped_column(
        String(20), default="active", comment="active/expired"
    )

    __table_args__ = (
        Index("uq_user_memberships_user", "user_id", unique=True),
        Index("ix_user_memberships_tenant", "tenant_id"),
    )
