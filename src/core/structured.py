# src/core/structured.py
#
# 统一的结构化输出入口（instructor + Pydantic）。
#
# 为什么需要单独一层：
# - 项目走 OpenAI 兼容网关（DashScope），其 function-calling 对嵌套 schema 支持不稳定，
#   langchain 的 with_structured_output 会返回 camelCase 字段名或干脆缺字段，
#   直接抛 ValidationError，调用点只能整体降级到本地兜底——结果就是"预约的医生/时段是随机编的"。
# - instructor 走 JSON 模式：把 schema 写进提示词，解析失败时把校验错误回灌给模型重试，
#   把"偶发格式错误"挡在业务层之前。
#
# 用法：
#     result = await extract_structured(AppointmentResult, prompt)

from __future__ import annotations

from typing import Any, TypeVar

from loguru import logger
from pydantic import BaseModel

from src.core.config import get_async_openai, get_settings

settings = get_settings()

ModelT = TypeVar("ModelT", bound=BaseModel)

# instructor 包装器：复用 config 里那个 bypass 代理的 AsyncOpenAI 连接，
# 不新建客户端，避免多一份连接池
_client: Any = None

DEFAULT_SYSTEM = (
    "你是严谨的医疗业务助手。请严格按给定 schema 以 JSON 格式输出，"
    "不要输出 JSON 以外的任何文字。"
)


def _get_client() -> Any:
    global _client
    if _client is None:
        import instructor

        # Mode.JSON：网关不支持严格 function calling 时的稳妥选择
        _client = instructor.from_openai(get_async_openai(), mode=instructor.Mode.JSON)
    return _client


async def extract_structured(
    response_model: type[BaseModel],
    prompt: str,
    *,
    system: str = DEFAULT_SYSTEM,
    model: str | None = None,
    temperature: float = 0.0,
    max_retries: int = 2,
) -> BaseModel:
    """让模型输出满足 response_model 的结构化结果。

    失败时抛异常，由调用方决定降级策略（本层不做静默兜底，
    否则业务方会误以为拿到了模型结果）。
    """
    client = _get_client()
    started = __import__("time").time()
    result = await client.chat.completions.create(
        model=model or settings.CHAT_MODEL,
        response_model=response_model,
        max_retries=max_retries,
        temperature=temperature,
        # 网关的 response_format=json_object 要求提示词里出现 "json" 字样
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    elapsed = round(__import__("time").time() - started, 2)
    logger.info(
        f"结构化输出成功 | schema={response_model.__name__} | 耗时={elapsed}s"
    )
    return result
