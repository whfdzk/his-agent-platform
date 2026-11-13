# src/modules/patient/service.py
#
# 患者端业务规则：体征评估、BMI、报告摘要。
#
# 放在 modules 层（不依赖 api），便于单测与复用；阈值口径集中在此，
# 前端只负责展示 level 对应的颜色，避免前后端各写一套判定。

from __future__ import annotations

from datetime import date, datetime

METRIC_META: dict[str, dict] = {
    "blood_pressure": {"label": "血压", "unit": "mmHg", "primary": "收缩压", "secondary": "舒张压"},
    "blood_glucose": {"label": "血糖", "unit": "mmol/L", "primary": "血糖", "secondary": ""},
    "weight": {"label": "体重", "unit": "kg", "primary": "体重", "secondary": ""},
    "heart_rate": {"label": "心率", "unit": "次/分", "primary": "心率", "secondary": ""},
}

LEVEL_LABELS = {"normal": "正常", "low": "偏低", "high": "偏高", "critical": "危急"}


def metric_label(metric_type: str) -> str:
    return METRIC_META.get(metric_type, {}).get("label", metric_type)


def evaluate_metric(
    metric_type: str,
    primary: float,
    secondary: float | None = None,
    period: str | None = None,
) -> str:
    """按通用成人参考区间给出 normal/low/high/critical。

    这是健康管理的提示分级，不构成诊断；危急值会同时触发 Safety Engine 的
    血压/血糖规则（规则库独立于本函数，避免一处改动影响两套判断）。
    """
    if metric_type == "blood_pressure":
        sys_v = float(primary)
        dia_v = float(secondary or 0)
        if sys_v >= 180 or dia_v >= 120:
            return "critical"
        if sys_v >= 140 or dia_v >= 90:
            return "high"
        if sys_v < 90 or (dia_v and dia_v < 60):
            return "low"
        return "normal"

    if metric_type == "blood_glucose":
        value = float(primary)
        if value >= 16.7 or value < 2.8:
            return "critical"
        if period == "fasting":
            if value >= 7.0:
                return "high"
            if value < 3.9:
                return "low"
            return "normal"
        # 随机 / 餐后
        if value >= 11.1:
            return "high"
        if value < 3.9:
            return "low"
        return "normal"

    if metric_type == "heart_rate":
        value = float(primary)
        if value >= 130 or value < 45:
            return "critical"

        if value > 100:
            return "high"
        if value < 55:
            return "low"
        return "normal"

    # 体重没有绝对正常区间，交由 BMI 判断；单次记录默认 normal
    return "normal"


def calc_bmi(height_cm: float | None, weight_kg: float | None) -> float | None:
    if not height_cm or not weight_kg or height_cm <= 0:
        return None
    h = height_cm / 100
    return round(weight_kg / (h * h), 1)


def bmi_level(bmi: float | None) -> str:
    if bmi is None:
        return "unknown"
    if bmi < 18.5:
        return "low"
    if bmi < 24:
        return "normal"
    if bmi < 28:
        return "high"
    return "critical"


DISCLAIMER_PERIOD = "以上为规则化统计提示，不能替代医生诊断；指标持续异常请线下就诊。"


def build_trend_chart(points: list[dict], unit: str = "", width: int = 24) -> str:
    """把时序点压成文本趋势图（对话渠道可用，前端另有 ECharts 图）。

    points 形如 [{"t": iso, "primary": 值, "secondary": 次值, "level": 分级}]。
    点数多于 width 时按等距抽样，保证首尾都在图里（首末点最能说明趋势）。
    """
    if not points:
        return "（暂无数据，无法生成趋势图）"

    values = [float(p["primary"]) for p in points if p.get("primary") is not None]
    if not values:
        return "（暂无有效数值，无法生成趋势图）"

    step = max(1, (len(values) + width - 1) // width)
    sampled = values[::step]
    if sampled[-1] != values[-1]:
        sampled.append(values[-1])

    blocks = "▁▂▃▄▅▆▇█"
    low, high = min(sampled), max(sampled)
    span = high - low or 1.0
    line = "".join(blocks[min(7, int((v - low) / span * 7.999))] for v in sampled)

    first, last = values[0], values[-1]
    delta = round(last - first, 1)
    direction = "上升" if delta > 0 else ("下降" if delta < 0 else "持平")
    return (
        f"{line}\n"
        f"首 {first:g}{unit} → 末 {last:g}{unit}（{direction} {abs(delta):g}{unit}）；"
        f"最高 {high:g}{unit}，最低 {low:g}{unit}，均值 {round(sum(values) / len(values), 1):g}{unit}，共 {len(values)} 次记录"
    )


def build_period_report(
    period_type: str,
    period_start: date,
    period_end: date,
    metrics: list[dict],
    plans: list[dict],
    medications: list[dict],
) -> tuple[str, str, dict]:
    """规则化周期健康报告：返回 (一句话摘要, 报告正文, 统计快照)。

    刻意不调 LLM：报告要可复现、可对比，模型的润色交给 Agent 回复层。
    """
    by_type: dict[str, list[dict]] = {}
    for m in metrics:
        by_type.setdefault(m["metric_type"], []).append(m)

    stats: dict = {
        "period_type": period_type,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "metrics": {},
        "record_count": len(metrics),
        "abnormal_count": sum(1 for m in metrics if m.get("level") not in (None, "normal")),
        "active_plans": sum(1 for p in plans if p.get("status") == "active"),
        "active_medications": sum(1 for m in medications if m.get("status") == "active"),
    }

    lines = [f"【{period_start} ~ {period_end} 健康报告（{period_type}）】"]
    if not metrics and not plans and not medications:
        lines.append("本周期暂无体征记录、健康计划与用药记录。建议先建立每日测量习惯。")
        return "本周期暂无健康数据，建议开始规律记录。", "\n".join(lines), stats

    for metric_type, rows in by_type.items():
        values = [float(r["primary"]) for r in rows if r.get("primary") is not None]
        abnormal = sum(1 for r in rows if r.get("level") not in (None, "normal"))
        entry = {
            "label": metric_label(metric_type),
            "count": len(rows),
            "unit": rows[0].get("unit") or METRIC_META.get(metric_type, {}).get("unit", ""),
            "avg": round(sum(values) / len(values), 1) if values else None,
            "max": max(values) if values else None,
            "min": min(values) if values else None,
            "abnormal_count": abnormal,
        }
        stats["metrics"][metric_type] = entry
        unit = entry["unit"]
        lines.append(
            f"\n{entry['label']}：{entry['count']} 次记录，均值 {entry['avg']}{unit}"
            f"（{entry['min']}~{entry['max']}{unit}），异常 {abnormal} 次"
        )
        lines.append(build_trend_chart(rows, unit=unit))

    if plans:
        active = [p for p in plans if p.get("status") == "active"]
        lines.append(f"\n健康计划：共 {len(plans)} 项，进行中 {len(active)} 项")
        for p in active[:5]:
            lines.append(f"- {p.get('title')}（目标：{p.get('target_text') or '未设定'}）")

    if medications:
        active = [m for m in medications if m.get("status") == "active"]
        lines.append(f"\n用药记录：共 {len(medications)} 条，在用 {len(active)} 条")
        for m in active[:5]:
            lines.append(f"- {m.get('drug_name')} {m.get('dosage') or ''} {m.get('frequency') or ''}".strip())

    total = stats["record_count"]
    ratio = round(stats["abnormal_count"] / total * 100) if total else 0
    summary = (
        f"本周期共 {total} 次体征记录，异常 {stats['abnormal_count']} 次（{ratio}%）；"
        f"在用计划 {stats['active_plans']} 项，在用药品 {stats['active_medications']} 种。"
    )
    lines.append("\n" + DISCLAIMER_PERIOD)
    return summary, "\n".join(lines), stats


def parse_datetime(value: str | None) -> datetime | None:
    """解析前端传入的 ISO 时间；容错 'YYYY-MM-DD HH:MM'。"""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def summarize_report(metrics: list[dict], report_type: str = "lab") -> str:
    """规则化报告解读：只描述异常项与建议，不做诊断、不输出原文。"""
    if not metrics:
        return "未从报告中识别到结构化指标。可上传更清晰的 PDF/图片，或手动录入关键指标。"

    abnormal = [m for m in metrics if m.get("abnormal") not in (None, "", "normal")]
    total = len(metrics)
    if not abnormal:
        return (
            f"本次共识别 {total} 项指标，均在参考区间内。"
            "建议保持规律作息与随访，具体以医生解读为准。"
        )

    high = [m for m in abnormal if m.get("abnormal") == "high"]
    low = [m for m in abnormal if m.get("abnormal") == "low"]
    others = [m for m in abnormal if m not in high and m not in low]

    parts = [f"本次共识别 {total} 项指标，其中 {len(abnormal)} 项异常。"]
    if high:
        parts.append("偏高：" + "、".join(f"{m['name']} {m.get('value_text', '')}{m.get('unit', '')}" for m in high[:5]) + "。")
    if low:
        parts.append("偏低：" + "、".join(f"{m['name']} {m.get('value_text', '')}{m.get('unit', '')}" for m in low[:5]) + "。")
    if others:
        parts.append("异常：" + "、".join(m["name"] for m in others[:5]) + "。")

    if report_type == "imaging":
        parts.append("影像报告描述需结合原始图像由医生判读，请携带报告面诊。")
    elif any(m.get("category") == "血脂" for m in abnormal):
        parts.append("血脂异常建议控制油脂摄入、增加有氧运动，并按医嘱复查。")
    elif any(m.get("category") == "血常规" for m in abnormal):
        parts.append("血常规异常需结合症状判断是否存在感染或贫血，建议 1-2 周复查。")
    else:
        parts.append("建议携带完整报告到相应专科复诊，由医生结合症状与既往史判断。")

    parts.append("以上为规则化提示，不能替代医生诊断。")
    return "".join(parts)
