"""语音接口：录音转文字（ASR）与回复朗读（TTS）。

两条硬约束：
- 音频不落库、不写磁盘、不进日志，只在内存里过一遍（与图片问答的既定口径一致）；
- 上游不可用时要给出可操作的错误，前端据此回退到键盘输入，而不是静默失败。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, UploadFile
from loguru import logger
from pydantic import BaseModel, Field
from starlette.responses import Response

from src.api.deps import get_current_user_id
from src.core.config import get_settings
from src.core.exceptions import BizErrorCode, BizException
from src.infra.voice import VoiceUnavailable, normalize_mime, synthesize_speech, transcribe_audio

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])


class TtsRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    voice: str | None = None


@router.get("/status")
async def voice_status() -> dict:
    """前端启动时探一次：关掉语音或模型不可用时，麦克风按钮直接给出提示。"""
    settings = get_settings()
    return {
        "enabled": settings.VOICE_ENABLED,
        "asr_model": settings.VOICE_ASR_MODEL,
        "tts_model": settings.VOICE_TTS_MODEL,
        "max_upload_bytes": settings.VOICE_MAX_UPLOAD_BYTES,
    }


@router.post("/asr")
async def asr(
    file: UploadFile = File(..., description="录音文件，前端统一发 16k 单声道 wav"),
    current_user: str = Depends(get_current_user_id),
) -> dict:
    """把一段录音转成文字，供输入框回填。"""
    settings = get_settings()
    if not settings.VOICE_ENABLED:
        raise BizException(BizErrorCode.CONFLICT, "语音功能已关闭", status_code=503)

    raw = await file.read()
    if not raw:
        raise BizException(BizErrorCode.PARAM_ERROR, "录音内容为空", status_code=400)
    if len(raw) > settings.VOICE_MAX_UPLOAD_BYTES:
        raise BizException(
            BizErrorCode.PARAM_ERROR,
            f"录音过大（{len(raw) // 1024} KB），请控制在 {settings.VOICE_MAX_UPLOAD_BYTES // (1024 * 1024)} MB 内",
            status_code=413,
        )

    mime = normalize_mime(file.content_type)
    t0 = time.monotonic()
    try:
        text = await transcribe_audio(raw, mime)
    except VoiceUnavailable as e:
        logger.warning(f"语音识别失败 user={current_user} bytes={len(raw)}: {e}")
        raise BizException(
            BizErrorCode.UPSTREAM_UNAVAILABLE,
            "语音识别暂时不可用，请改用键盘输入",
            status_code=502,
        ) from None

    cost_ms = int((time.monotonic() - t0) * 1000)
    # 只记体积、耗时和字数：音频内容与转写正文都不进日志
    logger.info(f"voice.asr user={current_user} bytes={len(raw)} mime={mime} cost={cost_ms}ms chars={len(text)}")

    if not text:
        raise BizException(
            BizErrorCode.PARAM_ERROR, "没有识别到语音内容，请再说一次", status_code=422,
        )
    return {"text": text, "cost_ms": cost_ms}


@router.post("/tts")
async def tts(
    payload: TtsRequest,
    current_user: str = Depends(get_current_user_id),
) -> Response:
    """把一段文字合成为 wav 直接回给前端播放（不返回外部 OSS 链接，避免过期与跨域）。"""
    settings = get_settings()
    if not settings.VOICE_ENABLED:
        raise BizException(BizErrorCode.CONFLICT, "语音功能已关闭", status_code=503)

    text = payload.text.strip()
    if not text:
        raise BizException(BizErrorCode.PARAM_ERROR, "朗读文本为空", status_code=400)
    if len(text) > settings.VOICE_MAX_TTS_CHARS:
        text = text[: settings.VOICE_MAX_TTS_CHARS]

    t0 = time.monotonic()
    try:
        audio, mime = await synthesize_speech(text, payload.voice)
    except VoiceUnavailable as e:
        logger.warning(f"语音合成失败 user={current_user}: {e}")
        raise BizException(
            BizErrorCode.UPSTREAM_UNAVAILABLE, "语音合成暂时不可用", status_code=502,
        ) from None

    cost_ms = int((time.monotonic() - t0) * 1000)
    logger.info(f"voice.tts user={current_user} chars={len(text)} bytes={len(audio)} cost={cost_ms}ms")
    return Response(
        content=audio,
        media_type=mime,
        headers={"Cache-Control": "no-store", "X-Cost-Ms": str(cost_ms)},
    )
