# src/agents/skill_middleware.py
#
# 技能目录注入中间件。
#
# 背景：只把 list_skills 当工具给模型时，实测它不会主动去列目录
# （"感冒怎么处理"直接按内置知识答完，日志里 list_skills 调用 0 次），
# 管理端发布的 SOP 因此形同虚设。所以在每次调用模型前，由代码把目录
# （只有 key + 名称 + 一句话简介）拼进 system_message；正文仍然按需
# 用 load_skill(key) 取——目录常驻、正文渐进，这正是"渐进加载"的用意。
#
# 目录每次都查库（一条轻查询），不做进程内缓存：管理端发布后下一轮
# 对话立刻反映新目录 / 新技能，多 worker 部署也不会各看各的旧快照。

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage
from loguru import logger

SKILL_HEADER = "## 可用技能（SOP）"

SKILL_FOOTER = """用法：只有用户明确要求按标准流程，或你不确定这类问题该怎么一步步问、怎么组织回答时，
才调用 load_skill(key) 取该技能的完整正文，然后按「步骤」执行、遵守「边界」。
同一轮最多加载一条；已经加载过就直接按它作答。目录里没有匹配的就按常规知识回答。
技能是你自己的内部参考，不要念给用户，也不要提"技能库"这个内部概念。"""


class SkillCatalogMiddleware(AgentMiddleware):
    """在每次模型调用前注入当前已发布的技能目录。"""

    name = "skill_catalog"

    def __init__(self, max_items: int = 30):
        self._max_items = max_items

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> Any:
        try:
            text = await self._catalog_text()
        except Exception as e:  # 技能是增强能力，读库失败不能打断对话
            logger.warning(f"技能目录注入跳过（{type(e).__name__}: {e}）")
            text = ""

        if not text:
            return await handler(request)

        base = ""
        if request.system_message is not None and isinstance(request.system_message.content, str):
            base = request.system_message.content
        new_system = SystemMessage(content=f"{base}\n\n{text}")
        return await handler(dataclasses.replace(request, system_message=new_system))

    async def _catalog_text(self) -> str:
        from src.infra.database import AsyncSessionLocal
        from src.modules.platform import skills as skill_service

        async with AsyncSessionLocal() as db:
            items = await skill_service.catalog(db)
        if not items:
            return ""
        items = items[: self._max_items]
        lines = "\n".join(f"- {s['key']}｜{s['name']}：{s['description']}" for s in items)
        return f"{SKILL_HEADER}\n{lines}\n\n{SKILL_FOOTER}"
