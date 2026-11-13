"""图片报告 OCR 兜底：确定性用例，mock 掉 MinerU 与视觉模型，不发真实请求。

覆盖此前的问题：jpg/png 只有 MinerU 一条路，未部署时拿到空文本，
extract_metrics 必然为空，前端显示"未识别到结构化指标"。
"""

import io

import pytest
from PIL import Image

from src.modules.patient import report_parser


def _fake_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1200), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def no_mineru(monkeypatch):
    """模拟 MinerU 未部署：真实链路里异常被吞成空串。"""

    async def _empty(content, filename):
        return ""

    monkeypatch.setattr(report_parser, "_mineru_text", _empty)


async def test_image_falls_back_to_vl_ocr(no_mineru, monkeypatch):
    async def _ocr(content, filename):
        assert filename.endswith(".jpg")
        return "心率 78 次/分 60-100"

    monkeypatch.setattr("src.infra.vl_ocr.ocr_image", _ocr)

    text, engine = await report_parser.extract_text(_fake_png(), "ecg.jpg")
    assert engine == "vl"
    assert "心率" in text


async def test_image_without_any_ocr_is_none(no_mineru, monkeypatch):
    async def _ocr(content, filename):
        return ""

    monkeypatch.setattr("src.infra.vl_ocr.ocr_image", _ocr)

    assert await report_parser.extract_text(_fake_png(), "ecg.jpg") == ("", "none")


async def test_vl_ocr_error_does_not_break_upload(no_mineru, monkeypatch):
    async def _ocr(content, filename):
        raise RuntimeError("视觉模型不支持图片输入")

    monkeypatch.setattr("src.infra.vl_ocr.ocr_image", _ocr)

    assert await report_parser.extract_text(_fake_png(), "ecg.jpg") == ("", "none")


async def test_txt_skips_ocr(monkeypatch, no_mineru):
    async def _boom(content, filename):
        raise AssertionError("纯文本不应触发 OCR")

    monkeypatch.setattr(report_parser, "_mineru_text", _boom)

    text, engine = await report_parser.extract_text("血红蛋白 120 g/L".encode(), "r.txt")
    assert engine == "text"
    assert "血红蛋白" in text


def test_vl_ocr_shrinks_image_long_edge():
    from src.infra.vl_ocr import _to_jpeg

    jpeg = _to_jpeg(_fake_png(), 1600)
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG"
        assert max(img.size) == 1600


def test_vl_ocr_flattens_segmented_content():
    from src.infra.vl_ocr import _flatten_content

    assert _flatten_content("心率 78") == "心率 78"
    assert _flatten_content([{"type": "text", "text": "心率 "}, {"type": "text", "text": "78"}]) == "心率 78"
    assert _flatten_content(None) == ""
