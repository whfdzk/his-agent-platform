"""报告解析与指标提取：确定性样例，不依赖 MinerU/pypdf。"""

from src.modules.patient.report_parser import extract_metrics, extract_meta
from src.modules.patient.service import evaluate_metric, summarize_report

SAMPLE = """某市第一人民医院 检验报告单
报告日期 2026-10-12
白细胞计数 11.2 10^9/L 3.5-9.5 ↑
血红蛋白 120 g/L 130-175 ↓
血小板 210 10^9/L 125-350
空腹血糖 7.8 mmol/L 3.9-6.1
低密度脂蛋白 3.9 mmol/L 1.8-3.4 H
"""


def test_extract_metrics_values_and_abnormal_flags():
    metrics = {m["code"]: m for m in extract_metrics(SAMPLE)}
    assert {"WBC", "HGB", "PLT", "GLU", "LDL"}.issubset(metrics.keys())
    assert metrics["WBC"]["abnormal"] == "high"
    assert metrics["HGB"]["abnormal"] == "low"
    # 单位里的 L 不能被当成"偏低"标记
    assert metrics["PLT"]["abnormal"] == "normal"
    assert metrics["GLU"]["value_num"] == 7.8
    assert metrics["GLU"]["abnormal"] == "high"
    assert metrics["LDL"]["abnormal"] == "high"


def test_english_abbrev_needs_word_boundary():
    """QTc 不能被当成总胆固醇，CRP 不能被当成肌酐。"""
    metrics = {m["code"]: m for m in extract_metrics("QTc 420 ms 350-440")}
    assert "TC" not in metrics

    metrics = {m["code"]: m for m in extract_metrics("C反应蛋白 12 mg/L 0-10")}
    assert "CREA" not in metrics
    assert metrics["CRP"]["value_num"] == 12


def test_longer_alias_wins_over_its_word_root():
    """「糖化血红蛋白」里的「血红蛋白」不能单独出一条指标。"""
    metrics = {m["code"]: m for m in extract_metrics("糖化血红蛋白 HbA1c 7.2 % 4.0-6.0")}
    assert "HGB" not in metrics
    assert metrics["HBA1C"]["value_num"] == 7.2


def test_extract_meta_guesses_hospital_and_date():
    hospital, report_date = extract_meta(SAMPLE)
    assert hospital and "医院" in hospital
    assert report_date is not None and report_date.year == 2026


def test_summary_mentions_abnormal_items_and_disclaimer():
    metrics = extract_metrics(SAMPLE)
    summary = summarize_report(metrics)
    assert "项指标" in summary
    assert "不能替代医生诊断" in summary


def test_metric_evaluation_thresholds():
    assert evaluate_metric("blood_pressure", 185, 125) == "critical"
    assert evaluate_metric("blood_pressure", 150, 95) == "high"
    assert evaluate_metric("blood_pressure", 118, 76) == "normal"
    assert evaluate_metric("blood_glucose", 2.5) == "critical"
    assert evaluate_metric("blood_glucose", 7.2, period="fasting") == "high"
    assert evaluate_metric("blood_glucose", 6.0, period="fasting") == "normal"
    assert evaluate_metric("heart_rate", 44) == "critical"
    assert evaluate_metric("heart_rate", 80) == "normal"
