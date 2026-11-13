# src/modules/ops/model_provider.py
#
# 候选模型链的读库实现：从 model_configs 读 chat 类型的可用配置，
# 排成「主模型 → 候选 1 → 候选 2 …」的故障转移顺序。
#
# 由 core/model_registry 反向调用（依赖倒置）：core 不 import 本模块，
# 而是由 main.py 的 lifespan 注入，见 set_provider。

from __future__ import annotations

from loguru import logger
from sqlalchemy import select

from src.core.model_registry import ModelCandidate
from src.infra.database import AsyncSessionLocal
from src.modules.ops.model import ModelConfig

# 只有 chat 类型参与故障转移：embedding / rerank / multimodal 的调用点不走 get_llm
CHAT_MODEL_TYPE = "chat"


def _sort_key(m: ModelConfig) -> tuple[int, int, int]:
    """默认模型排最前，其余按 priority 升序、同优先级按 id 升序（保证顺序稳定）。"""
    return (0 if m.is_default else 1, m.priority or 100, m.id or 0)


async def load_chat_candidates() -> list[ModelCandidate]:
    """读取启用中的 chat 配置，返回按故障转移顺序排列的候选链。"""
    try:
        async with AsyncSessionLocal() as db:
            rows = (
                await db.execute(
                    select(ModelConfig).where(
                        ModelConfig.model_type == CHAT_MODEL_TYPE,
                        ModelConfig.enabled.is_(True),
                    )
                )
            ).scalars().all()
    except Exception as e:
        logger.warning(f"读取候选模型失败（回退到 .env 单模型）: {type(e).__name__}: {e}")
        return []

    ordered = sorted(rows, key=_sort_key)
    return [
        ModelCandidate(
            name=m.name,
            model_name=m.model_name,
            base_url=m.base_url or "",
            api_key=m.api_key or "",
        )
        for m in ordered
    ]
