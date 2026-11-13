# src/modules/patient/schema.py
#
# 患者端 API 的请求体模型（响应统一用 dict，便于按页面组合字段）。

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MetricType = Literal["blood_pressure", "blood_glucose", "weight", "heart_rate"]


class MetricIn(BaseModel):
    metric_type: MetricType
    value_primary: float = Field(..., description="主值：血压为收缩压")
    value_secondary: float | None = Field(None, description="次值：血压为舒张压")
    unit: str | None = Field(None, max_length=16)
    period: str | None = Field(None, max_length=20, description="fasting/postprandial/random")
    note: str | None = Field(None, max_length=200)
    measured_at: str | None = Field(None, description="ISO 时间，缺省为当前时间")


class ReminderIn(BaseModel):
    title: str = Field(..., max_length=100)
    metric_type: MetricType | None = None
    remind_time: str = Field("08:00", max_length=8)
    frequency: Literal["daily", "weekly"] = "daily"
    weekdays: str | None = Field(None, max_length=20)
    enabled: bool = True
    note: str | None = Field(None, max_length=200)


class ReminderUpdate(BaseModel):
    title: str | None = Field(None, max_length=100)
    remind_time: str | None = Field(None, max_length=8)
    frequency: Literal["daily", "weekly"] | None = None
    weekdays: str | None = Field(None, max_length=20)
    enabled: bool | None = None
    note: str | None = Field(None, max_length=200)


class ProfileUpdate(BaseModel):
    """健康档案：基础身份写 patients，扩展字段写 patient_profiles。"""

    name: str | None = Field(None, max_length=100)
    gender: str | None = Field(None, max_length=10)
    age: int | None = Field(None, ge=0, le=120)
    birth_date: str | None = None
    blood_type: str | None = Field(None, max_length=10)
    height_cm: float | None = Field(None, ge=30, le=250)
    weight_kg: float | None = Field(None, ge=2, le=400)
    waist_cm: float | None = Field(None, ge=20, le=200)
    marital_status: str | None = Field(None, max_length=20)
    occupation: str | None = Field(None, max_length=50)
    city: str | None = Field(None, max_length=50)
    emergency_contact_name: str | None = Field(None, max_length=50)
    emergency_contact_phone: str | None = Field(None, max_length=20)
    allergy_history: str | None = Field(None, max_length=2000)
    medical_history: str | None = Field(None, max_length=4000)
    surgery_history: str | None = Field(None, max_length=4000)
    family_history: str | None = Field(None, max_length=2000)
    chronic_diseases: str | None = Field(None, max_length=1000)
    smoking: Literal["never", "former", "current"] | None = None
    drinking: Literal["never", "occasional", "regular"] | None = None


class ConsentUpdate(BaseModel):
    scope: Literal["ai_consult", "data_share", "research", "marketing"]
    granted: bool
    version: str = Field("v1", max_length=20)


class MedicationIn(BaseModel):
    """用药记录：agent 与前端共用同一套入参校验。"""

    drug_name: str = Field(..., max_length=100)
    spec: str | None = Field(None, max_length=50)
    dosage: str | None = Field(None, max_length=50)
    frequency: str | None = Field(None, max_length=50)
    indication: str | None = Field(None, max_length=200)
    start_date: str | None = None
    end_date: str | None = None
    status: Literal["active", "stopped", "finished"] = "active"
    note: str | None = Field(None, max_length=300)


class HealthPlanIn(BaseModel):
    title: str = Field(..., max_length=100)
    plan_type: Literal["diet", "exercise", "medication", "monitor", "other"] = "other"
    metric_type: MetricType | None = None
    target_text: str | None = Field(None, max_length=200)
    frequency: str | None = Field(None, max_length=50)
    start_date: str | None = None
    end_date: str | None = None
    note: str | None = Field(None, max_length=300)


class HealthPlanUpdate(BaseModel):
    title: str | None = Field(None, max_length=100)
    target_text: str | None = Field(None, max_length=200)
    frequency: str | None = Field(None, max_length=50)
    end_date: str | None = None
    status: Literal["active", "completed", "abandoned"] | None = None
    note: str | None = Field(None, max_length=300)
