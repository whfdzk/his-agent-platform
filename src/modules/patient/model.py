# ============================================================
# 患者端领域 Model：健康档案 / 体征指标 / 报告 / 提醒 / 隐私授权
#
# 设计约定：
# - 所有表都以 patient_id 做行级隔离，接口层 user_id 只来自 JWT，禁止请求体自报
# - 报告原文（PDF 文本）、身份证号、手机号等敏感字段一律不进日志，
#   报告原文仅存 RustFS，本表只存结构化指标与摘要
# ============================================================

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin


class PatientProfile(BaseModel, TenantMixin):
    """患者健康档案扩展：patients 表存基础身份，本表存生活方式与手术/家族史。"""

    __tablename__ = "patient_profiles"

    patient_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("patients.id", ondelete="CASCADE"),
        nullable=False,
        comment="患者 ID（一人一条档案）",
    )
    birth_date: Mapped[date | None] = mapped_column(Date, comment="出生日期")
    height_cm: Mapped[float | None] = mapped_column(Float, comment="身高（cm）")
    weight_kg: Mapped[float | None] = mapped_column(Float, comment="体重（kg）")
    waist_cm: Mapped[float | None] = mapped_column(Float, comment="腰围（cm）")
    marital_status: Mapped[str | None] = mapped_column(String(20), comment="婚姻状况")
    occupation: Mapped[str | None] = mapped_column(String(50), comment="职业")
    city: Mapped[str | None] = mapped_column(String(50), comment="常住城市")
    emergency_contact_name: Mapped[str | None] = mapped_column(String(50), comment="紧急联系人")
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(20), comment="紧急联系人电话")
    surgery_history: Mapped[str | None] = mapped_column(Text, comment="手术史，多条用换行分隔")
    family_history: Mapped[str | None] = mapped_column(Text, comment="家族史")
    chronic_diseases: Mapped[str | None] = mapped_column(Text, comment="慢性病标签，逗号分隔")
    smoking: Mapped[str | None] = mapped_column(String(20), comment="吸烟：never/former/current")
    drinking: Mapped[str | None] = mapped_column(String(20), comment="饮酒：never/occasional/regular")

    __table_args__ = (
        UniqueConstraint("tenant_id", "patient_id", name="uq_patient_profiles_tenant_patient"),
        Index("ix_patient_profiles_tenant", "tenant_id"),
    )


class MedicationRecord(BaseModel, TenantMixin):
    """患者用药记录：MedicationAgent 的「用户用药记录管理」落库表。

    只记录患者自述/医生开具的用药事实，不做剂量审核结论（审核走处方审核工具）。
    """

    __tablename__ = "medication_records"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    drug_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="药品名称")
    spec: Mapped[str | None] = mapped_column(String(50), comment="规格，如 0.5g×24 片")
    dosage: Mapped[str | None] = mapped_column(String(50), comment="单次剂量，如 1 片 / 10mg")
    frequency: Mapped[str | None] = mapped_column(String(50), comment="用药频次，如 每日三次 / qd")
    indication: Mapped[str | None] = mapped_column(String(200), comment="用药目的/适应症")
    start_date: Mapped[date | None] = mapped_column(Date, comment="开始日期")
    end_date: Mapped[date | None] = mapped_column(Date, comment="结束日期（空表示长期服用）")
    status: Mapped[str] = mapped_column(
        String(16), default="active", comment="状态：active=在用 / stopped=已停 / finished=已完成"
    )
    source: Mapped[str] = mapped_column(
        String(20), default="manual", comment="来源：manual=自述 / prescription=处方 / import=导入"
    )
    note: Mapped[str | None] = mapped_column(String(300), comment="备注（不含敏感信息）")

    __table_args__ = (
        Index("ix_medication_records_patient_status", "patient_id", "status"),
        Index("ix_medication_records_patient_drug", "patient_id", "drug_name"),
        Index("ix_medication_records_tenant", "tenant_id"),
    )


class HealthPlan(BaseModel, TenantMixin):
    """健康计划：慢病管理的长期目标（控盐、减重、每日步数、定期复查等）。"""

    __tablename__ = "health_plans"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="计划标题")
    plan_type: Mapped[str] = mapped_column(
        String(20), default="other", comment="类型：diet/exercise/medication/monitor/other"
    )
    metric_type: Mapped[str | None] = mapped_column(String(32), comment="关联指标类型（可空）")
    target_text: Mapped[str | None] = mapped_column(String(200), comment="目标描述，如 3 个月减重 5kg")
    frequency: Mapped[str | None] = mapped_column(String(50), comment="执行频次，如 每日 / 每周三次")
    start_date: Mapped[date | None] = mapped_column(Date, comment="开始日期")
    end_date: Mapped[date | None] = mapped_column(Date, comment="结束日期")
    status: Mapped[str] = mapped_column(
        String(16), default="active", comment="状态：active/completed/abandoned"
    )
    note: Mapped[str | None] = mapped_column(String(300), comment="备注")

    __table_args__ = (
        Index("ix_health_plans_patient_status", "patient_id", "status"),
        Index("ix_health_plans_tenant", "tenant_id"),
    )


class HealthPeriodReport(BaseModel, TenantMixin):
    """周期健康报告：按周/月汇总体征趋势与用药依从性，生成后可重复查看。

    报告正文由规则化统计生成（不调 LLM），保证可复现；LLM 只在 Agent 回复时润色。
    """

    __tablename__ = "health_period_reports"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    period_type: Mapped[str] = mapped_column(String(16), default="week", comment="周期：week/month")
    period_start: Mapped[date] = mapped_column(Date, nullable=False, comment="周期起始日")
    period_end: Mapped[date] = mapped_column(Date, nullable=False, comment="周期结束日")
    summary: Mapped[str | None] = mapped_column(Text, comment="一句话摘要")
    content: Mapped[str | None] = mapped_column(Text, comment="报告正文（规则化生成）")
    stats: Mapped[dict | None] = mapped_column(JSONB, comment="统计快照 JSON：各指标均值/异常次数")

    __table_args__ = (
        Index("ix_health_period_reports_patient", "patient_id", "period_end"),
        Index("ix_health_period_reports_tenant", "tenant_id"),
    )


class VitalMetric(BaseModel, TenantMixin):
    """体征指标：血压 / 血糖 / 体重 / 心率统一存储。

    value_primary / value_secondary 的语义：
    - blood_pressure：primary=收缩压，secondary=舒张压
    - 其它类型：primary=测量值，secondary 为空
    """

    __tablename__ = "vital_metrics"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    metric_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="类型：blood_pressure/blood_glucose/weight/heart_rate"
    )
    value_primary: Mapped[float] = mapped_column(Float, nullable=False, comment="主值")
    value_secondary: Mapped[float | None] = mapped_column(Float, comment="次值（血压舒张压）")
    unit: Mapped[str] = mapped_column(String(16), default="", comment="单位")
    period: Mapped[str | None] = mapped_column(
        String(20), comment="测量时段：fasting/postprandial/random/morning/evening"
    )
    level: Mapped[str] = mapped_column(
        String(16), default="normal", comment="评估：normal/high/low/critical"
    )
    source: Mapped[str] = mapped_column(
        String(20), default="manual", comment="来源：manual/device/report"
    )
    note: Mapped[str | None] = mapped_column(String(200), comment="备注（不含敏感信息）")
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="测量时间"
    )

    __table_args__ = (
        Index("ix_vital_metrics_patient_type_time", "patient_id", "metric_type", "measured_at"),
        Index("ix_vital_metrics_tenant", "tenant_id"),
        Index("ix_vital_metrics_tenant_patient_measured", "tenant_id", "patient_id", "measured_at"),
    )


class HealthReport(BaseModel, TenantMixin):
    """患者上传的检查报告。

    原文只存 RustFS（object_key），DB 不落报告全文，避免敏感信息扩散到查询/日志链路。
    """

    __tablename__ = "health_reports"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="原始文件名")
    object_key: Mapped[str] = mapped_column(String(500), nullable=False, comment="RustFS 对象 key")
    file_size: Mapped[int] = mapped_column(Integer, default=0, comment="文件大小（字节）")
    mime_type: Mapped[str] = mapped_column(String(100), default="", comment="MIME 类型")
    report_type: Mapped[str] = mapped_column(
        String(32), default="lab", comment="类型：lab=检验/imaging=影像/physical=体检/other"
    )
    hospital: Mapped[str | None] = mapped_column(String(100), comment="报告机构")
    report_date: Mapped[date | None] = mapped_column(Date, comment="报告日期")
    status: Mapped[str] = mapped_column(
        String(20), default="uploaded", comment="状态：uploaded/parsing/parsed/failed"
    )
    parse_engine: Mapped[str | None] = mapped_column(String(32), comment="解析引擎：mineru/pypdf/text/vl")
    summary: Mapped[str | None] = mapped_column(Text, comment="规则化解读摘要（不含原文）")
    abnormal_count: Mapped[int] = mapped_column(Integer, default=0, comment="异常指标数量")
    metric_count: Mapped[int] = mapped_column(Integer, default=0, comment="识别指标数量")
    error_message: Mapped[str | None] = mapped_column(String(300), comment="解析失败原因（已脱敏）")
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="解析完成时间")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="软删除时间")

    __table_args__ = (
        Index("ix_health_reports_patient_time", "patient_id", "created_at"),
        Index("ix_health_reports_status", "status"),
        Index("ix_health_reports_tenant", "tenant_id"),
    )


class ReportMetric(BaseModel, TenantMixin):
    """报告结构化指标：用于详情展示与跨报告对比。"""

    __tablename__ = "report_metrics"

    report_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("health_reports.id", ondelete="CASCADE"), nullable=False, comment="报告 ID"
    )
    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID（冗余便于对比）"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="指标名称")
    code: Mapped[str | None] = mapped_column(String(50), comment="指标编码（对比用，如 HGB）")
    category: Mapped[str] = mapped_column(String(50), default="其他", comment="分类：血常规/生化/血脂/尿常规等")
    value_text: Mapped[str] = mapped_column(String(50), default="", comment="原始值文本（如 5.2 或 阴性）")
    value_num: Mapped[float | None] = mapped_column(Float, comment="数值化取值")
    unit: Mapped[str] = mapped_column(String(30), default="", comment="单位")
    ref_low: Mapped[float | None] = mapped_column(Float, comment="参考下限")
    ref_high: Mapped[float | None] = mapped_column(Float, comment="参考上限")
    ref_text: Mapped[str | None] = mapped_column(String(100), comment="参考区间原文")
    abnormal: Mapped[str] = mapped_column(
        String(16), default="normal", comment="normal/high/low/abnormal"
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="展示顺序")

    __table_args__ = (
        Index("ix_report_metrics_report", "report_id"),
        Index("ix_report_metrics_patient_code", "patient_id", "code"),
        Index("ix_report_metrics_tenant", "tenant_id"),
    )


class HealthReminder(BaseModel, TenantMixin):
    """健康管理提醒（测量/用药/复诊）。"""

    __tablename__ = "health_reminders"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    metric_type: Mapped[str | None] = mapped_column(String(32), comment="关联指标类型")
    title: Mapped[str] = mapped_column(String(100), nullable=False, comment="提醒标题")
    remind_time: Mapped[str] = mapped_column(String(8), default="08:00", comment="提醒时间 HH:MM")
    frequency: Mapped[str] = mapped_column(String(16), default="daily", comment="daily/weekly")
    weekdays: Mapped[str | None] = mapped_column(String(20), comment="weekly 时的星期，逗号分隔 1-7")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否启用")
    note: Mapped[str | None] = mapped_column(String(200), comment="备注")
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="最近触发时间")

    __table_args__ = (
        Index("ix_health_reminders_patient", "patient_id"),
        Index("ix_health_reminders_tenant", "tenant_id"),
    )


class PrivacyConsent(BaseModel, TenantMixin):
    """隐私授权记录：每次授权/撤回都追加一条，保留审计轨迹。"""

    __tablename__ = "privacy_consents"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="授权范围：ai_consult/data_share/research/marketing"
    )
    granted: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否授权")
    version: Mapped[str] = mapped_column(String(20), default="v1", comment="隐私政策版本")
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="授权时间")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="撤回时间")

    __table_args__ = (
        Index("ix_privacy_consents_patient_scope", "patient_id", "scope"),
        Index("ix_privacy_consents_tenant", "tenant_id"),
    )


class DataRequest(BaseModel, TenantMixin):
    """数据导出 / 账号删除申请：需人工在保留期内完成。"""

    __tablename__ = "data_requests"

    patient_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, comment="患者 ID"
    )
    request_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="export=导出 / delete=删除"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", comment="pending/processing/completed/rejected"
    )
    object_key: Mapped[str | None] = mapped_column(String(500), comment="导出文件 RustFS key")
    note: Mapped[str | None] = mapped_column(String(300), comment="处理说明")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="导出文件过期时间")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="处理完成时间")

    __table_args__ = (
        Index("ix_data_requests_patient", "patient_id", "created_at"),
        Index("ix_data_requests_tenant", "tenant_id"),
    )
