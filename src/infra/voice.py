"""语音能力：ASR（语音转文字）与 TTS（文字转语音）。

网关（DashScope 兼容模式）不实现 OpenAI 的 /audio/transcriptions 与 /audio/speech，
实测可用路径只有两条，代码按这两条实现：

- ASR：POST /chat/completions，content 里塞 {"type":"input_audio","input_audio":{"data":<data URL>}}，
  模型 qwen3.5-omni-flash（唯一一次性转写中文有效的；qwen-audio-3.0-asr-flash 免费额度耗尽，
  fun-asr-flash 报 format is empty，qwen3-asr-flash-realtime 一次性 HTTP 直接 500）。
- TTS：DashScope 原生 /api/v1/services/aigc/multimodal-generation/generation，
  模型 qwen3-tts-flash，返回 output.audio.url（有时效 OSS 链接），后端下载成字节返回前端播放。

音频只在内存流转：不写磁盘、不落库、不进日志（只记字节数与耗时）。
"""
from __future__ import annotations

import base64
import struct
from urllib.parse import urlparse

import httpx
from loguru import logger

from src.core.config import _resolve_chat_credentials, get_settings

# 可接受的音频 MIME（前端统一发 16k 单声道 wav，其余为兜底）
_ALLOWED_MIME = {
    "audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave",
    "audio/webm", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac",
    "application/octet-stream",
}

_ASR_PROMPT = (
    "请把这段语音原样转写成简体中文文字。"
    "只输出转写结果本身，不要回答问题，不要解释，不要加引号或“转写结果：”之类前缀。"
)


class VoiceUnavailable(Exception):
    """语音服务不可用（模型不支持 / 额度用尽 / 上游异常），路由层转 502。"""


def normalize_mime(mime: str | None) -> str:
    """归一化音频 MIME；不认识的一律按 wav 处理（前端实际只发 wav）。"""
    m = (mime or "").split(";")[0].strip().lower()
    return m if m in _ALLOWED_MIME else "audio/wav"


def _clean_transcript(text: str) -> str:
    """模型偶尔会给结果加引号或“转写结果：”前缀，这里统一剥掉。"""
    t = (text or "").strip()
    for prefix in ("转写结果：", "转写结果:", "转写内容：", "转写内容:"):
        if t.startswith(prefix):
            t = t[len(prefix):].strip()
    return t.strip().strip('"“”\'')


async def transcribe_audio(raw: bytes, mime: str) -> str:
    """语音 → 文字。失败抛 VoiceUnavailable，由调用方决定如何降级。"""
    settings = get_settings()
    api_key, base_url = _resolve_chat_credentials()
    data_url = f"data:{normalize_mime(mime)};base64,{base64.b64encode(raw).decode()}"

    payload = {
        "model": settings.VOICE_ASR_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio", "input_audio": {"data": data_url}},
                {"type": "text", "text": _ASR_PROMPT},
            ],
        }],
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.VOICE_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except httpx.HTTPError as e:
        raise VoiceUnavailable(f"语音识别请求失败: {type(e).__name__}") from e

    if resp.status_code != 200:
        detail = resp.text[:200].replace("\n", " ")
        logger.warning(f"ASR 上游非 200: {resp.status_code} {detail}")
        raise VoiceUnavailable(f"语音识别服务返回 {resp.status_code}")

    try:
        content = resp.json()["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, ValueError) as e:
        raise VoiceUnavailable("语音识别返回结构异常") from e

    return _clean_transcript(content)


def _native_endpoint(base_url: str) -> str:
    """兼容模式 base 与原生服务同 host，只是路径不同。"""
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}/api/v1/services/aigc/multimodal-generation/generation"


def _normalize_wav(raw: bytes) -> bytes:
    """修掉流式合成留下的占位长度。

    qwen3-tts 返回的 wav 头里 RIFF 与 data 长度都是接近 int32 上限的占位值
    （边合成边写、无从知道总长）。浏览器一般按 EOF 硬播还能出声，但按头部
    计算时长的播放器会把它当成 44739 秒的音频，切进度条直接卡死。
    这里按真实字节数重写这两个字段。
    """
    if len(raw) < 44 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw

    offset = 12
    while offset + 8 <= len(raw):
        chunk_id = raw[offset:offset + 4]
        chunk_size = struct.unpack("<I", raw[offset + 4:offset + 8])[0]
        if chunk_id == b"data":
            real = len(raw) - offset - 8
            if chunk_size == real:
                return raw
            fixed = bytearray(raw)
            fixed[4:8] = struct.pack("<I", len(raw) - 8)
            fixed[offset + 4:offset + 8] = struct.pack("<I", real)
            return bytes(fixed)
        if chunk_size == 0 or chunk_size > len(raw):
            break
        offset += 8 + chunk_size
    return raw


async def synthesize_speech(text: str, voice: str | None = None) -> tuple[bytes, str]:
    """文字 → 音频字节。返回 (wav 字节, mime)。"""
    settings = get_settings()
    api_key, base_url = _resolve_chat_credentials()
    body = {
        "model": settings.VOICE_TTS_MODEL,
        "input": {"text": text},
        "parameters": {"voice": voice or settings.VOICE_TTS_VOICE},
    }
    try:
        async with httpx.AsyncClient(timeout=settings.VOICE_TIMEOUT) as client:
            resp = await client.post(
                _native_endpoint(base_url),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
            if resp.status_code != 200:
                detail = resp.text[:200].replace("\n", " ")
                logger.warning(f"TTS 上游非 200: {resp.status_code} {detail}")
                raise VoiceUnavailable(f"语音合成服务返回 {resp.status_code}")

            audio = (resp.json().get("output") or {}).get("audio") or {}
            if audio.get("data"):
                return _normalize_wav(base64.b64decode(audio["data"])), "audio/wav"
            url = audio.get("url")
            if not url:
                raise VoiceUnavailable("语音合成未返回音频")
            # OSS 链接有时效，立刻取回二进制交给前端，避免前端缓存到过期地址
            got = await client.get(url)
            if got.status_code != 200:
                raise VoiceUnavailable(f"语音文件下载失败 {got.status_code}")
            return _normalize_wav(got.content), "audio/wav"
    except httpx.HTTPError as e:
        raise VoiceUnavailable(f"语音合成请求失败: {type(e).__name__}") from e
