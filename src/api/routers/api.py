import time
from fastapi import APIRouter, Depends
from loguru import logger

from src.api.deps import get_current_admin
from src.core.exceptions import BizErrorCode, BizException

from src.core.config import get_async_openai, get_settings

# 诊断端点只给管理端用：无鉴权时会变成"免费调用大模型 + 探测内网网关"的入口
router = APIRouter(
    prefix="/medical", tags=["medical"], dependencies=[Depends(get_current_admin)],
)


@router.get("/llm/test")
async def llm_connection(request_mode: str = "chat"):
    """探活用的诊断端点（仅管理员）：直连 LLM 网关验证 Key / base_url / 模型是否可用。"""
    start = time.time()
    try:
        client = get_async_openai()
        settings = get_settings()
        model = settings.DEEPSEEK_MODEL

        ## 经典请求模式
        if request_mode == "chat":
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个医疗助手。"},
                    {"role": "user", "content": "请回复：连接成功！"},
                ],
                max_tokens=50,
            )
            content = response.choices[0].message.content
        else:
            ## 响应模式，轨迹流动暂时不支持
            response = await client.responses.create(
                model=settings.CHAT_MODEL,
                input=[
                    {"role": "system", "content": "你是一个写作助手。"},
                    {"role": "user", "content": "请回复：连接成功！"},
                ],
                max_output_tokens=50,
            )
            content = response.output_text

        elapsed = round(time.time() - start, 2)

        # 回复内容可能包含患者信息，日志只记录长度与耗时
        logger.info(f"LLM 测试成功 | model={model} | 耗时={elapsed}s | 回复长度={len(content or '')}")

        return {
            "status": "ok",
            "model": model,
            "base_url": settings.BASE_URL_CHAT,
            "elapsed_seconds": elapsed,
            "reply": content,
        }

    except BizException:
        raise
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        # 原始异常只进日志，不回传客户端（可能含 Key / 内网地址）
        logger.error(f"LLM 测试失败 | 耗时={elapsed}s | error={e}")
        raise BizException(
            BizErrorCode.UPSTREAM_UNAVAILABLE, "大模型请求失败，请检查网关配置", status_code=502,
        ) from None