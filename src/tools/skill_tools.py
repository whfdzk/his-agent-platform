# src/tools/skill_tools.py
#
# Agent Harness 的技能层：把处理经验写成 SOP，模型先看目录、需要时再加载正文。
#
# 为什么拆两步：
#   8 条 SOP 全文塞进系统提示词，既占上下文又互相干扰（问诊的规则会误导用药回答）。
#   目录只给"key + 一句简介"，模型判断与当前问题相关后再 load_skill 取全文，
#   这就形成了"渐进加载"：上下文占用从"全部技能"降到"命中的那一两条"。
#
# 热更新：正文存在 skills / skill_versions 表，管理端发布后下一轮对话即生效，
# 不进代码、不重启服务。

from __future__ import annotations

from langchain_core.tools import tool
from loguru import logger


@tool
async def list_skills() -> str:
    """列出当前已发布的技能目录（每条只有一句话简介）。

    使用时机：你需要判断"这个诉求该按哪套 SOP 处理"时先调用它。
    不要在开头无脑列出全部技能给用户看；它只用于你自己决定下一步加载哪条。
    拿到目录后，用 load_skill(key) 取真正要用的那条正文。
    """
    try:
        from src.infra.database import AsyncSessionLocal
        from src.modules.platform import skills as skill_service

        async with AsyncSessionLocal() as db:
            items = await skill_service.catalog(db)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"技能目录读取失败: {type(e).__name__}: {e}")
        return "技能目录暂时不可用，按通用流程处理。"

    if not items:
        return "当前没有已发布的技能，按通用流程处理。"
    logger.info(f"技能目录命中 {len(items)} 条：{[s['key'] for s in items]}")
    lines = "\n".join(f"- {s['key']}｜{s['name']}：{s['description']}" for s in items)
    return f"已发布技能（共 {len(items)} 条）：\n{lines}"


@tool
async def load_skill(key: str) -> str:
    """按 key 加载一条技能的正文（SKILL.md 全文）。

    只在确实需要按某套 SOP 处理时调用，一次只加载一条；
    加载后按正文里的步骤与边界执行，不要自行改写其中的硬性规则。
    key 用 list_skills 返回的英文标识（如 inquiry、medication_safety）。
    """
    try:
        from src.infra.database import AsyncSessionLocal
        from src.modules.platform import skills as skill_service

        async with AsyncSessionLocal() as db:
            content, version = await skill_service.get_body(db, key)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"技能正文读取失败 key={key}: {type(e).__name__}: {e}")
        return f"技能 {key} 读取失败，按通用流程处理。"

    if not content:
        return f"技能 {key} 不存在或尚未发布，按通用流程处理。"
    logger.info(f"技能正文加载 key={key} v={version} len={len(content)}")
    return f"【技能 {key} v{version}】\n{content}"
