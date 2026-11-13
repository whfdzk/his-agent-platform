# 统一的 Embedding 客户端工厂。
#
# 此前 supervisor_agent / inquiry graph / knowledge router 各自 new 一个
# OpenAIEmbeddings（参数完全一致、base_url 各自硬编码），换供应商要改 3 处业务
# 模块 + 3 个脚本。现在统一从这里取进程级单例。

from __future__ import annotations

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from src.core.config import get_settings
from src.core.exceptions import BizErrorCode, BizException


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """返回进程级共享的 Embedding 客户端。

    非 OpenAI 官方模型必须禁用 tiktoken 分块（check_embedding_ctx_length=False），
    否则长文本会被截断或直接报错。
    """
    settings = get_settings()
    if not settings.EMBEDDING_API_KEY:
        raise BizException(
            BizErrorCode.CONFIG_MISSING,
            "未配置 EMBEDDING_API_KEY，请在 .env 中设置（用于长期记忆与知识库向量化）",
        )

    return OpenAIEmbeddings(
        model=settings.EMBEDDING_MODEL,
        api_key=settings.EMBEDDING_API_KEY,
        base_url=settings.EMBEDDING_BASE_URL or None,
        check_embedding_ctx_length=False,
    )
