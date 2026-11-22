# src/modules/clinician/model.py
#
# 医护侧的"我能看谁"关系表（C4）。
#
# 为什么不能只靠角色判断：
#   `clinician` 角色只说明"这个账号是医生"，不回答"他能看哪些患者的记录"。
#   如果按角色直接放行全量，等于全院医生都能翻任意患者的急症记录与对话 ——
#   对医疗数据来说这是越权，C1 上多租户之后还得推倒重做。
#
# 两张表分别回答两件事：
#   clinician_departments：他属于哪些科室（能看本科室范围内的急症告警）
#   clinician_patients   ：哪些患者由他负责（能看这些患者的完整记录）
#
# 授权规则（由 scope.py 实现，接口层不得自行拼判定）：
#   1. 责任患者 → 该患者的急症事件全等级可读；
#   2. 非责任患者 → 仅当该患者最近就诊科室属于医生绑定科室，且事件为 red/orange（高危才跨患者告警）；
#   3. 其余一律不可读。

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.core.base_model import BaseModel, TenantMixin

# 责任关系的来源
RELATION_ASSIGNED = "assigned"     # 主动分配（我的患者）
RELATION_CONSULTED = "consulted"   # 接诊后自动建立

# 医生档案的出诊状态
PROFILE_ACTIVE = "active"          # 可被挂号 / 出现在医生目录
PROFILE_INACTIVE = "inactive"      # 停诊（档案保留，不再出现在目录）


class ClinicianDepartment(BaseModel, TenantMixin):
    """医生 ↔ 科室绑定。一个医生可绑定多个科室，其中一个是主科室。"""

    __tablename__ = "clinician_departments"

    clinician_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="医护账号（patients.id）"
    )
    department_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="departments.id"
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否主科室"
    )
    granted_by: Mapped[str] = mapped_column(
        String(64), default="", comment="授予人，如 admin:1 / system:seed"
    )
    note: Mapped[str] = mapped_column(String(255), default="", comment="备注")

    __table_args__ = (
        UniqueConstraint("tenant_id", "clinician_id", "department_id",
                         name="uq_clinician_departments_tenant"),
        Index("ix_clinician_departments_clinician", "clinician_id"),
        Index("ix_clinician_departments_department", "department_id"),
        Index("ix_clinician_departments_tenant", "tenant_id"),
    )


class ClinicianPatient(BaseModel, TenantMixin):
    """医生 ↔ 责任患者。"""

    __tablename__ = "clinician_patients"

    clinician_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="医护账号（patients.id）"
    )
    patient_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="patients.id"
    )
    relation: Mapped[str] = mapped_column(
        String(24), default=RELATION_ASSIGNED, nullable=False,
        comment="关系来源：assigned / consulted",
    )
    department_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="建立关系时所属科室"
    )
    granted_by: Mapped[str] = mapped_column(
        String(64), default="", comment="建立人或来源"
    )
    note: Mapped[str] = mapped_column(String(255), default="", comment="备注")

    __table_args__ = (
        UniqueConstraint("clinician_id", "patient_id", name="uq_clinician_patients"),
        Index("ix_clinician_patients_clinician", "clinician_id"),
        Index("ix_clinician_patients_patient", "patient_id"),
        Index("ix_clinician_patients_tenant", "tenant_id"),
    )


class ClinicianProfile(BaseModel, TenantMixin):
    """医生档案：把"appointments.doctor_name 那串自由文本"变成可映射的医生账号。

    为什么单独一张表而不是直接塞进 patients：
      patients 是账号表（手机号、密码、启用状态），档案是"出诊信息"（职称、专长、工号、
      是否在岗）。两者的生命周期不同 —— 停诊不该停用账号，注销账号也不该丢档案。
      合在一起会让患者端账号查询把医生的出诊信息也带出去。

    它和 clinician_departments 的分工：
      科室绑定回答"他能看哪些科室的告警"（授权）；档案里的 specialty 只是展示文案。
      也就是说，档案不参与授权判定，scope.py 不读这张表。
    """

    __tablename__ = "clinician_profiles"

    clinician_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="医护账号（patients.id）"
    )
    employee_no: Mapped[str] = mapped_column(
        String(32), default="", comment="工号"
    )
    title: Mapped[str] = mapped_column(
        String(64), default="", comment="职称，如 主任医师 / 主治医师"
    )
    specialty: Mapped[str] = mapped_column(
        String(128), default="", comment="专长，如 冠心病介入"
    )
    intro: Mapped[str] = mapped_column(
        String(255), default="", comment="一句话简介（患者端选医生时展示）"
    )
    status: Mapped[str] = mapped_column(
        String(20), default=PROFILE_ACTIVE, nullable=False,
        comment="出诊状态：active=在岗 / inactive=停诊",
    )
    note: Mapped[str] = mapped_column(String(255), default="", comment="备注")

    __table_args__ = (
        UniqueConstraint("tenant_id", "clinician_id",
                         name="uq_clinician_profiles_tenant_clinician"),
        Index("ix_clinician_profiles_tenant", "tenant_id"),
    )
