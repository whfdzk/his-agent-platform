# src/infra/vl_ocr.py
#
# 图片报告 OCR 兜底：MinerU 未部署时，用视觉大模型把图片报告转录成文本。
#
# 为什么需要这一层：
#   report_parser 里 PDF 有 pypdf 本地兜底，而 jpg/png/webp 只有 MinerU 一条路，
#   MinerU 是可选外部服务，未部署时图片拿到空文本 → extract_metrics 返回空 →
#   前端显示"未识别到结构化指标"。VL 让图片至少有一次可用的 OCR 机会。
#
# 触发顺序（见 report_parser.extract_text）：MinerU → VL。
#   MinerU 在版面/表格还原上更稳，先让它跑；它不可用才退化到视觉模型。
#
# 隐私：图片只在内存中转 base64 后发给模型，不落库、不进日志；
#   异常只记录类型名与截断的异常摘要，绝不回显识别出的报告文本。

from __future__ import annotations

import base64
import io
from pathlib import Path

from langchain_core.messages import HumanMessage
from loguru import logger

from src.core.config import get_settings, get_vl_llm
from src.core.concurrency import guarded, to_thread

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# 逐行转录而非总结：下游 extract_metrics 靠"指标名 + 数值"同行匹配，
# 让模型改写措辞会直接把指标弄丢。
_OCR_PROMPT = (
    "请将这张中文医疗检查报告图片中的文字完整、逐行转录出来。\n"
    "要求：\n"
    "1. 只输出转录文本，保留原有行结构：指标名、数值、单位、参考区间保持在同一行；\n"
    "2. 不要总结、不要解释、不要翻译，也不要添加图片中不存在的任何内容；\n"
    "3. 不要输出 Markdown 代码块标记或表格分隔线；\n"
    "4. 表格类报告按行展开成「名称 值 单位 参考区间」的形式；\n"
    "5. 若图片中没有文字或不是医疗报告，只输出空内容。"
)


def _to_jpeg(content: bytes, max_side: int) -> bytes:
    """同步压缩图片（调用方放线程池）：统一 JPEG，长边压到 max_side。

    视觉模型按图片 token 计费且对长边有上限，手机拍的报告常是 3000+ 像素，
    直接上传既慢又容易触发网关 413。
    """
    from PIL import Image

    with Image.open(io.BytesIO(content)) as img:
        img = img.convert("RGB")  # png/webp 可能带 alpha，JPEG 不支持
        width, height = img.size
        scale = max(width, height) / max_side
        if scale > 1:
            img = img.resize((int(width / scale), int(height / scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()


def _flatten_content(content: object) -> str:
    """部分网关把回复切成 [{'type': 'text', 'text': ...}] 分段列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return ""


async def ocr_image(content: bytes, filename: str) -> str:
    """识别图片报告文字，返回纯文本；不可用或失败一律返回空串（不阻断上传）。"""
    settings = get_settings()
    if not settings.VL_OCR_ENABLED:
        return ""
    if Path(filename).suffix.lower() not in IMAGE_EXTS:
        return ""

    try:
        jpeg = await to_thread(_to_jpeg, content, settings.VL_OCR_MAX_SIDE)
    except Exception as e:
        logger.warning(f"图片预处理失败，跳过 VL OCR: {type(e).__name__}: {str(e)[:100]}")
        return ""

    data_uri = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
    message = HumanMessage(
        content=[
            {"type": "text", "text": _OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
    )
    try:
        # 走全局 LLM 信号量，避免批量上传把网关打满
        result = await guarded(
            get_vl_llm().ainvoke([message]), timeout=settings.VL_OCR_TIMEOUT
        )
    except Exception as e:
        logger.warning(f"VL OCR 不可用，跳过: {type(e).__name__}: {str(e)[:100]}")
        return ""

    text = _flatten_content(getattr(result, "content", "")).strip()
    if text:
        logger.info(f"VL OCR 识别成功: {len(text)} 字符")
    return text


# ── 聊天图片问答：把患者上传的图片理解成文字 ─────────────────────────────
#
# 与 ocr_image 的分工：
#   ocr_image：报告录入链路，要求"逐行转录"，下游靠行结构提取指标；
#   describe_medical_image：聊天链路，图片类型不限（报告单/药盒/皮疹/伤口），
#   要的是"能喂给纯文本 Agent 的可读描述"，所以要按类型分别描述重点。
#
# 为什么不直接把图片塞给主模型：项目主干 Agent 绑的是纯文本 chat 模型
# （DeepSeek 一类不支持 image 输入）。先转文字再进对话，既不用换模型、
# 也不用改 Agent 装配，且图片内容会作为普通文本参与后续检索与安全审核。
#
# 隐私：图片只在内存中转 base64，不落库、不进日志；失败只记异常摘要。

_QA_PROMPT = (
    "你在辅助一个医疗问诊系统理解患者上传的图片。请客观描述图中与健康相关的内容，"
    "供接诊医生助理参考：\n"
    "1. 先说明这是什么图（检查/检验报告单、药品包装或说明书、皮肤或伤口照片、其他）；\n"
    "2. 报告单或说明书：逐项列出可见的项目名、数值、单位、参考区间；\n"
    "3. 皮肤或伤口：描述部位、形态、颜色、范围等可见特征；\n"
    "4. 只描述看到的，不要推测图片以外的信息，不要给诊断结论或用药建议；\n"
    "5. 中文条目式输出，控制在 200 字以内。"
)


def _decode_image_payload(payload: str) -> bytes:
    """接受 data URL 或裸 base64 两种入参，统一解成原始字节。"""
    raw = (payload or "").strip()
    if raw.startswith("data:"):
        comma = raw.find(",")
        if comma < 0:
            raise ValueError("data URL 缺少逗号分隔符")
        raw = raw[comma + 1:]
    return base64.b64decode(raw, validate=False)


async def describe_medical_image(payload: str, filename: str = "", question: str = "") -> str:
    """把患者随消息上传的图片转成文字描述；不可用或失败一律返回空串（不阻断对话）。"""
    settings = get_settings()
    if not settings.IMAGE_QA_ENABLED or not payload:
        return ""

    try:
        content = _decode_image_payload(payload)
    except Exception as e:
        logger.warning(f"图片解码失败，跳过图片理解: {type(e).__name__}: {str(e)[:100]}")
        return ""
    if len(content) > settings.IMAGE_QA_MAX_BYTES:
        logger.warning(f"图片过大（{len(content)} 字节），跳过图片理解")
        return ""

    try:
        jpeg = await to_thread(_to_jpeg, content, settings.VL_OCR_MAX_SIDE)
    except Exception as e:
        logger.warning(f"图片预处理失败，跳过图片理解: {type(e).__name__}: {str(e)[:100]}")
        return ""

    prompt = _QA_PROMPT
    if question.strip():
        prompt += f"\n患者随图提出的是：{question.strip()[:200]}"

    data_uri = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
    )
    try:
        result = await guarded(
            get_vl_llm().ainvoke([message]), timeout=settings.IMAGE_QA_TIMEOUT
        )
    except Exception as e:
        logger.warning(f"图片理解不可用，跳过: {type(e).__name__}: {str(e)[:100]}")
        return ""

    text = _flatten_content(getattr(result, "content", "")).strip()
    if text:
        logger.info(f"图片理解成功: {len(text)} 字符（来源 {filename or '未命名'}）")
    return text[:800]
