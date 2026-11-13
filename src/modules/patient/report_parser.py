# src/modules/patient/report_parser.py
#
# 检查报告解析：PDF/图片 → 文本 → 结构化指标。
#
# 解析链路：
#   1. PDF 优先用 pypdf 抽文本（快、无外部依赖）
#   2. 文本过少（扫描件 / 图片）→ MinerU OCR（可选依赖，失败则降级）
#   3. 图片在 MinerU 不可用时 → 视觉大模型 OCR 兜底（见 src/infra/vl_ocr.py）
#   4. 规则化提取常见检验指标：名称 / 值 / 单位 / 参考区间 / 异常标记
#
# 隐私：解析得到的原文只在内存中使用，不落库、不写日志；DB 只存结构化指标与摘要。

from __future__ import annotations

import io
import os
import re
import tempfile
from pathlib import Path

from loguru import logger

from src.core.concurrency import to_thread

# 常见检验指标目录（覆盖血常规 / 生化 / 血脂 / 炎症 / 甲状腺）
LAB_CATALOG: list[dict] = [
    {"code": "WBC", "name": "白细胞计数", "aliases": ("白细胞", "WBC"), "unit": "10^9/L", "category": "血常规", "low": 3.5, "high": 9.5},
    {"code": "RBC", "name": "红细胞计数", "aliases": ("红细胞计数", "RBC"), "unit": "10^12/L", "category": "血常规", "low": 4.3, "high": 5.8},
    {"code": "HGB", "name": "血红蛋白", "aliases": ("血红蛋白", "HGB", "Hb"), "unit": "g/L", "category": "血常规", "low": 130, "high": 175},
    {"code": "PLT", "name": "血小板计数", "aliases": ("血小板", "PLT"), "unit": "10^9/L", "category": "血常规", "low": 125, "high": 350},
    {"code": "NEUT%", "name": "中性粒细胞百分比", "aliases": ("中性粒细胞百分比", "中性粒细胞%", "NEUT%"), "unit": "%", "category": "血常规", "low": 40, "high": 75},
    {"code": "LYM%", "name": "淋巴细胞百分比", "aliases": ("淋巴细胞百分比", "淋巴细胞%", "LYM%"), "unit": "%", "category": "血常规", "low": 20, "high": 50},
    {"code": "ALT", "name": "丙氨酸氨基转移酶", "aliases": ("丙氨酸氨基转移酶", "谷丙转氨酶", "ALT"), "unit": "U/L", "category": "肝功能", "low": 7, "high": 40},
    {"code": "AST", "name": "天门冬氨酸氨基转移酶", "aliases": ("天门冬氨酸氨基转移酶", "谷草转氨酶", "AST"), "unit": "U/L", "category": "肝功能", "low": 13, "high": 35},
    {"code": "GGT", "name": "γ-谷氨酰转移酶", "aliases": ("谷氨酰转移酶", "γ-GT", "GGT"), "unit": "U/L", "category": "肝功能", "low": 7, "high": 45},
    {"code": "TBIL", "name": "总胆红素", "aliases": ("总胆红素", "TBIL"), "unit": "μmol/L", "category": "肝功能", "low": 5, "high": 21},
    {"code": "CREA", "name": "肌酐", "aliases": ("肌酐", "CREA", "Cr"), "unit": "μmol/L", "category": "肾功能", "low": 57, "high": 97},
    {"code": "BUN", "name": "尿素", "aliases": ("尿素氮", "尿素", "BUN"), "unit": "mmol/L", "category": "肾功能", "low": 3.1, "high": 8.0},
    {"code": "UA", "name": "尿酸", "aliases": ("尿酸", "UA"), "unit": "μmol/L", "category": "肾功能", "low": 208, "high": 428},
    {"code": "GLU", "name": "空腹血糖", "aliases": ("空腹血糖", "血糖", "GLU"), "unit": "mmol/L", "category": "血糖", "low": 3.9, "high": 6.1},
    {"code": "HBA1C", "name": "糖化血红蛋白", "aliases": ("糖化血红蛋白", "HbA1c", "HBA1C"), "unit": "%", "category": "血糖", "low": 4.0, "high": 6.0},
    {"code": "TC", "name": "总胆固醇", "aliases": ("总胆固醇", "TC", "CHOL"), "unit": "mmol/L", "category": "血脂", "low": 2.8, "high": 5.2},
    {"code": "TG", "name": "甘油三酯", "aliases": ("甘油三酯", "TG"), "unit": "mmol/L", "category": "血脂", "low": 0.4, "high": 1.7},
    {"code": "LDL", "name": "低密度脂蛋白胆固醇", "aliases": ("低密度脂蛋白", "LDL"), "unit": "mmol/L", "category": "血脂", "low": 1.8, "high": 3.4},
    {"code": "HDL", "name": "高密度脂蛋白胆固醇", "aliases": ("高密度脂蛋白", "HDL"), "unit": "mmol/L", "category": "血脂", "low": 1.0, "high": 1.8},
    {"code": "CRP", "name": "C 反应蛋白", "aliases": ("C反应蛋白", "C 反应蛋白", "CRP"), "unit": "mg/L", "category": "炎症", "low": 0, "high": 10},
    {"code": "TSH", "name": "促甲状腺激素", "aliases": ("促甲状腺激素", "TSH"), "unit": "mIU/L", "category": "甲状腺", "low": 0.27, "high": 4.2},
]

# 数字不能粘在字母后面：否则 "糖化血红蛋白 HbA1c 7.2" 会取到 HbA1c 里的 1
_NUM_RE = re.compile(r"(?<![A-Za-z0-9.])-?\d+(?:\.\d+)?")
_REF_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~—－至]\s*(\d+(?:\.\d+)?)")
_DATE_RE = re.compile(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})")
_HOSPITAL_RE = re.compile(r"([\u4e00-\u9fa5]{2,20}(?:医院|medical center|hospital))", re.IGNORECASE)

MAX_TEXT_CHARS = 20000


def _alias_pattern(alias: str) -> re.Pattern:
    """英文缩写要求词边界，中文别名仍用子串匹配。

    纯子串匹配会把别的指标名误吞：QTc 命中 TC（总胆固醇）、CRP 命中 Cr（肌酐）、
    HbA1c 命中 Hb（血红蛋白）。心电/糖化报告里这些缩写同现，误判出来的指标数值是错的，
    比识别不出来更危险。LDL-C 这种连字符写法仍要能命中，故只排除字母与数字邻接。
    """
    if alias.isascii():
        return re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE
        )
    return re.compile(re.escape(alias), re.IGNORECASE)


_ALIAS_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    item["code"]: [(a, _alias_pattern(a)) for a in item["aliases"]]
    for item in LAB_CATALOG
}


def _pdf_text(content: bytes) -> str:
    """同步抽取 PDF 文本（调用方放线程池）。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages[:20]:
            pages.append(page.extract_text() or "")
        return "\n".join(pages)
    except Exception as e:
        logger.warning(f"pypdf 解析失败: {e}")
        return ""


def _write_temp(content: bytes, suffix: str) -> str:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        return tmp.name


async def _mineru_text(content: bytes, filename: str) -> str:
    """扫描件/图片走 MinerU OCR；服务不可用时返回空串（不阻断上传）。"""
    suffix = Path(filename).suffix.lower() or ".pdf"
    tmp_path = await to_thread(_write_temp, content, suffix)
    try:
        from src.infra.mineru_client import parse_document

        return await parse_document(tmp_path, filename)
    except Exception as e:
        logger.warning(f"MinerU 解析不可用，跳过 OCR: {e}")
        return ""
    finally:
        try:
            await to_thread(os.unlink, tmp_path)
        except Exception:
            pass


async def _vl_text(content: bytes, filename: str) -> str:
    """图片 OCR 兜底：MinerU 不可用时用视觉模型转录；失败返回空串。"""
    try:
        from src.infra.vl_ocr import ocr_image

        return await ocr_image(content, filename)
    except Exception as e:
        logger.warning(f"VL OCR 不可用，跳过: {type(e).__name__}: {str(e)[:100]}")
        return ""


async def extract_text(content: bytes, filename: str) -> tuple[str, str]:
    """返回 (文本, 引擎名)。文本仅用于本次解析，不落库。"""
    suffix = Path(filename).suffix.lower()

    # 纯文本直接解码：此前该分支排在 OCR 之后，txt 也会白跑一次 MinerU 请求
    if suffix in (".txt", ".md"):
        return content.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS], "text"

    if suffix == ".pdf":
        text = await to_thread(_pdf_text, content)
        if len(text.strip()) >= 60:
            return text[:MAX_TEXT_CHARS], "pypdf"

    ocr = await _mineru_text(content, filename)
    if ocr.strip():
        return ocr[:MAX_TEXT_CHARS], "mineru"

    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        vl = await _vl_text(content, filename)
        if vl.strip():
            return vl[:MAX_TEXT_CHARS], "vl"

    return "", "none"


def _pick_value(line: str, after_index: int) -> float | None:
    """取指标名之后的第一个数值（排除参考区间里的数字）。"""
    tail = line[after_index:]
    # 先截掉参考区间部分，避免把区间下限当成测量值
    ref = _REF_RE.search(tail)
    head = tail[: ref.start()] if ref else tail
    match = _NUM_RE.search(head)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def extract_metrics(text: str) -> list[dict]:
    """从报告文本中提取结构化指标。"""
    if not text:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found: dict[str, dict] = {}

    for line in lines:
        compact = re.sub(r"\s+", " ", line)
        hits: list[tuple[dict, str, re.Match]] = []
        for item in LAB_CATALOG:
            if item["code"] in found:
                continue
            for alias, pattern in _ALIAS_PATTERNS[item["code"]]:
                hit = pattern.search(compact)
                if hit:
                    hits.append((item, alias, hit))
                    break

        for item, alias, alias_hit in hits:
            # 词根被同一行里更长的别名包含时丢弃短的那个：
            # "糖化血红蛋白" 里的 "血红蛋白" 不能单独出一条血红蛋白指标
            if any(other != alias and alias in other for _, other, _ in hits):
                continue
            idx = alias_hit.end()
            value = _pick_value(compact, idx)
            if value is None:
                continue

            ref_match = _REF_RE.search(compact[idx:])
            ref_low = float(ref_match.group(1)) if ref_match else item["low"]
            ref_high = float(ref_match.group(2)) if ref_match else item["high"]

            abnormal = "normal"
            # 只把独立列上的 H/L 当异常标记：单位里的 "mmol/L"、"10^9/L" 不能误判
            if "↑" in compact or re.search(r"(?<![/\w])H(?![\w/])", compact):
                abnormal = "high"
            elif "↓" in compact or re.search(r"(?<![/\w])L(?![\w/])", compact):
                abnormal = "low"
            elif value > ref_high:
                abnormal = "high"
            elif value < ref_low:
                abnormal = "low"

            unit_match = re.search(r"(10\^\d+/L|g/L|mg/L|mmol/L|μmol/L|U/L|%|mIU/L)", compact)
            found[item["code"]] = {
                "name": item["name"],
                "code": item["code"],
                "category": item["category"],
                "value_text": f"{value:g}",
                "value_num": value,
                "unit": unit_match.group(1) if unit_match else item["unit"],
                "ref_low": ref_low,
                "ref_high": ref_high,
                "ref_text": f"{ref_low:g}-{ref_high:g}",
                "abnormal": abnormal,
            }

    return sorted(found.values(), key=lambda m: (m["category"], m["name"]))


def extract_meta(text: str) -> tuple[str | None, object]:
    """从文本里猜报告机构与日期（识别不到返回 None）。"""
    hospital = None
    date_value = None
    m = _HOSPITAL_RE.search(text or "")
    if m:
        hospital = m.group(1)[:100]
    d = _DATE_RE.search(text or "")
    if d:
        try:
            from datetime import date

            date_value = date(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        except ValueError:
            date_value = None
    return hospital, date_value


async def parse_report(content: bytes, filename: str) -> dict:
    """完整解析：返回 {text, engine, metrics, hospital, report_date}。"""
    text, engine = await extract_text(content, filename)
    metrics = extract_metrics(text)
    hospital, report_date = extract_meta(text)
    return {
        "text": text,
        "engine": engine,
        "metrics": metrics,
        "hospital": hospital,
        "report_date": report_date,
    }
