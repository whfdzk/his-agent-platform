# ============================================================
# 运维/平台管理数据 Model 定义
#
# - TokenUsage：每次 LLM 调用一行，由 src/core/llm_callbacks.py 埋点写入
# - ModelConfig：管理端维护的模型配置（嵌入/聊天/重排/多模态），本期重启后生效
# ============================================================

from sqlalchemy import String, Integer, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel


class TokenUsage(BaseModel):
    __tablename__ = "token_usage"

    user_id: Mapped[str | None] = mapped_column(String(100), comment="用户 ID", nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(100), comment="会话 ID", nullable=True)
    turn_id: Mapped[str | None] = mapped_column(
        String(64), comment="轮次 ID：同一次用户消息触发的多次 LLM 调用共享", nullable=True
    )
    worker: Mapped[str] = mapped_column(
        String(50), default="supervisor", comment="调用来源：supervisor/inquiry/drug/knowledge/report/operation"
    )
    model: Mapped[str] = mapped_column(String(100), default="", comment="模型名称")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输入 token 数")
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="输出 token 数")
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, comment="总 token 数")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, comment="本次调用耗时（毫秒）")

    __table_args__ = (
        Index("ix_token_usage_turn", "turn_id"),
        Index("ix_token_usage_session", "session_id"),
        Index("ix_token_usage_worker", "worker"),
        # 统计接口按时间过滤 + 按天分组
        Index("ix_token_usage_created_at", "created_at"),
    )


class ModelConfig(BaseModel):
    __tablename__ = "model_configs"

    model_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="模型类型：embedding/chat/rerank/multimodal"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="展示名称")
    base_url: Mapped[str] = mapped_column(String(500), default="", comment="API Base URL")
    api_key: Mapped[str] = mapped_column(String(500), default="", comment="API Key（本期明文存储）")
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="模型名称，如 deepseek-chat")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否该类型的默认配置")
    # 故障转移顺序：同类型内数字越小越先被尝试；is_default 的那条永远排在最前，
    # 其余按 (priority, id) 升序排成候选链，主模型失败时依次切换。
    priority: Mapped[int] = mapped_column(
        Integer, default=100, comment="故障转移优先级，数字越小越先尝试"
    )

    __table_args__ = (
        Index("ix_model_configs_type", "model_type"),
        Index("ix_model_configs_type_priority", "model_type", "priority"),
    )
