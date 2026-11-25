"""技能（SOP）配置表：skills + skill_versions，并灌入 8 条内置技能。

Revision ID: b8c4e2f6a1d3
Revises: a7b3c9d1e5f2
Create Date: 2026-11-25

设计口径与 prompts / prompt_versions 一致：
- 元数据与正文分离，正文是不可变快照；
- 发布 / 回滚只改 skills.active_version；
- 首次迁移即把内置 8 条技能写入并发布 v1，保证升级后行为不变
  （此前是 tool_registry.SKILL_CATALOG 常量，只做目录展示，没有正文）。
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8c4e2f6a1d3"
down_revision = "a7b3c9d1e5f2"
branch_labels = None
depends_on = None


SEEDS = [
    (
        "inquiry", "症状问诊与分诊", "收集症状信息并提供就诊分诊建议",
        ["call_inquiry_agent"], "挂载：Supervisor 路由到问诊子 Agent",
        """# 症状问诊与分诊

## 适用
患者描述身体不适、询问该看哪个科。

## 步骤
1. 先确认三件事：持续多久、具体部位、有无伴随症状（发热 / 出血 / 意识改变）。
2. 出现红旗症状（胸痛伴大汗、言语不清、单侧肢体无力、意识障碍、剧烈腹痛、大出血）时，立即走急症闸门：先给"立即拨打 120 / 就近急诊"，不再继续问诊。
3. 已知信息够两条即可给科室建议，不要无限追问；科室要落到具体科室（心内科 / 呼吸内科 / 消化内科 / 神经内科等），不要只回答"内科"。
4. 同一症状 24 小时内反复咨询，直接建议线下就诊，不要重复同一套问诊。

## 边界
- 不下诊断，不回答"我是不是得了某病"。
- 不替代急诊，不说"应该没事"这类保证性结论。
- 患者拒绝提供信息时，按现有信息给最低风险建议并提示就医。
""",
    ),
    (
        "medication_safety", "用药安全", "药物咨询、相互作用识别与处方审查",
        ["call_drug_agent", "query_drug_info", "check_drug_allergy_risk",
         "manage_medication_record", "review_prescription_tool"], "挂载：用药子 Agent",
        """# 用药安全

## 适用
患者问药怎么吃、能不能一起吃、漏服怎么办、有没有副作用。

## 步骤
1. 先确认药名与剂型（缓释 / 控释 / 普通片不能互换），再谈剂量。
2. 剂量只引用说明书或知识库已有条目；知识库没有时明确说"未收录，请以说明书或药师意见为准"，不要凭经验编剂量。
3. 涉及抗凝（华法林）、降糖（胰岛素 / 磺脲）、降压、镇静类时，主动提示相互作用与监测要点。
4. 患者提到过敏史时，先查档案过敏记录再回答；冲突时优先提示禁忌。
5. 儿童、孕产妇、肝肾功能不全者的剂量问题，一律建议线下由医生确定。

## 边界
- 不开处方、不调剂量、不建议停药或换药。
- 急症（过量服用、误服、意识改变）直接给急救指引并转人工。
""",
    ),
    (
        "medical_knowledge", "医学知识检索", "基于医学文档库与知识图谱回答专业问题",
        ["call_knowledge_agent", "search_knowledge_docs", "search_knowledge_graph",
         "search_knowledge_multi", "search_knowledge_agentic", "list_knowledge_sources"],
        "挂载：知识子 Agent",
        """# 医学知识检索

## 适用
患者问疾病机制、诊断标准、检查意义、指南推荐等知识性问题。

## 步骤
1. 先检索再回答，不要凭记忆直接下结论；优先用多路检索拿到证据。
2. 回答里区分"指南/说明书原文"与"通俗解释"，关键数值（阈值、剂量、分期）必须来自检索结果。
3. 给出出处意识：说明依据来自知识库文档或图谱关系，不虚构文献名。
4. 检索为空时如实说"资料库暂无该内容"，给通用建议并提示就医，不要编造。

## 边界
- 不做个体化诊断与预后判断。
- 不解读患者本人的检查单数值（走报告解读技能）。
""",
    ),
    (
        "report_interpretation", "检查报告解读", "结合结构化指标解释检查报告",
        ["call_report_agent", "get_recent_reports"], "挂载：报告子 Agent",
        """# 检查报告解读

## 适用
患者上传或询问化验单、影像报告里的指标含义。

## 步骤
1. 先取结构化指标（项目名、数值、单位、参考区间），缺参考区间时按常见区间说明并标注"以本院报告为准"。
2. 逐项说明：是否超出参考区间、可能相关的系统、需要结合哪些其他检查。
3. 多项异常同时出现时，指出它们可能指向同一方向，不要逐条吓人。
4. 给出下一步建议：复查时间、挂哪个科、需要带什么资料。

## 边界
- 不下诊断结论，不对影像做定性判断（如"是不是肿瘤"）。
- 危急值（如血钾极高、血糖极低、血红蛋白极低）立即提示急诊。
""",
    ),
    (
        "health_management", "健康管理", "管理健康指标、趋势和提醒",
        ["call_health_agent", "record_vital_metric", "get_metric_trends",
         "manage_health_plan", "manage_reminder", "generate_period_report"],
        "挂载：健康子 Agent",
        """# 健康管理

## 适用
患者记录血压 / 血糖 / 体重，想看趋势、设提醒、要周期报告。

## 步骤
1. 记录指标时先确认数值、单位、测量时间与状态（空腹 / 餐后 / 静息）。
2. 谈趋势前先看历史点位数量；少于 5 个点只描述事实，不判定"控制得好/不好"。
3. 提醒与计划要落到可执行动作（几点、做什么、多久一次），不要给笼统的"注意饮食"。
4. 周期报告汇总：本期均值、达标比例、异常次数、下期建议。

## 边界
- 不调整药物方案。
- 指标持续异常时建议线下复诊，不在对话里"确诊"。
""",
    ),
    (
        "care_guidance", "就医流程辅助", "提供科室、医院与就医流程信息",
        ["call_guide_agent", "query_department_info", "recommend_department",
         "get_visit_preparation", "organize_visit_materials", "query_hospital_info",
         "manage_appointment"], "挂载：就医子 Agent",
        """# 就医流程辅助

## 适用
患者问去哪家医院、挂什么科、要带什么、怎么预约。

## 步骤
1. 先确认城市与就诊类型（初诊 / 复诊 / 专病门诊），再给医院与科室建议。
2. 就诊准备按"证件 + 既往资料 + 当前用药 + 症状时间线"四类给清单。
3. 预约类操作必须走确认流程：先复述科室、时间、医院，患者确认后才提交。
4. 号源或医院信息查不到时如实告知，给替代方案（电话咨询 / 现场挂号）。

## 边界
- 不承诺"能挂到号"。
- 急诊情形直接给急救指引，不走预约流程。
""",
    ),
    (
        "operations_analytics", "医院运营分析", "面向运营人员查询统计数据",
        ["call_operation_agent", "search_knowledge_sql"], "挂载：运营子 Agent（仅运营角色）",
        """# 医院运营分析

## 适用
运营 / 管理人员问就诊量、科室分布、转化率等统计口径。

## 步骤
1. 先确认时间范围与统计口径（按人次 / 按会话 / 按订单）。
2. 只查聚合结果，不返回患者明细；涉及个人信息的字段一律脱敏或不返回。
3. 给出数字时同时说明口径与时间窗，避免跨口径比较。
4. 数据为空时说明"该区间无数据"，不要用估算值填充。

## 边界
- 不预测业务走势、不做经营建议结论。
- 不做跨租户数据对比。
""",
    ),
    (
        "patient_memory", "患者记忆与档案", "长期记忆读写与患者档案查询",
        ["save_memory", "search_memory", "get_my_profile"], "挂载：Supervisor 常驻",
        """# 患者记忆与档案

## 适用
需要回忆患者既往偏好、长期病史，或查询本人档案字段。

## 步骤
1. 对话开始遇到"我之前说过""老毛病"这类指代时，先 search_memory 再回答。
2. 新获得的稳定事实（慢性病、长期用药、过敏、复诊节奏）在回答结束后 save_memory，只存一句结论。
3. 档案查询只返回本人可见字段；手机号、身份证等敏感字段不主动展示。
4. 记忆与档案冲突时以档案为准，并说明"以档案记录为准"。

## 边界
- 不把一次性的情绪、临时症状写进长期记忆。
- 不记忆与医疗无关的隐私内容。
""",
    ),
]


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("key", sa.String(64), nullable=False, comment="技能键"),
        sa.Column("name", sa.String(100), nullable=False, comment="展示名称"),
        sa.Column("description", sa.String(500), nullable=False, server_default="",
                  comment="一句话简介"),
        sa.Column("tool_ids", postgresql.JSONB(), nullable=True, comment="声明的工具名"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("active_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mount_hint", sa.String(200), nullable=False, server_default=""),
    )
    op.create_index("uq_skills_key", "skills", ["key"], unique=True)

    op.create_table(
        "skill_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()")),
        sa.Column("skill_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, comment="SKILL.md 正文"),
        sa.Column("change_note", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("published_at", sa.String(32), nullable=True),
    )
    op.create_index("uq_skill_versions_skill_version", "skill_versions",
                    ["skill_id", "version"], unique=True)

    published_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    bind = op.get_bind()
    for key, name, desc, tools, hint, body in SEEDS:
        skill_id = bind.execute(
            sa.text(
                "INSERT INTO skills (key, name, description, tool_ids, enabled, active_version, mount_hint) "
                "VALUES (:key, :name, :desc, CAST(:tools AS JSONB), true, 1, :hint) RETURNING id"
            ),
            {"key": key, "name": name, "desc": desc, "tools": str(tools).replace("'", '"'),
             "hint": hint},
        ).scalar()
        bind.execute(
            sa.text(
                "INSERT INTO skill_versions (skill_id, version, content, change_note, created_by, published_at) "
                "VALUES (:sid, 1, :content, '内置技能初始化', 'system', :pub)"
            ),
            {"sid": skill_id, "content": body, "pub": published_at},
        )


def downgrade() -> None:
    op.drop_index("uq_skill_versions_skill_version", table_name="skill_versions")
    op.drop_table("skill_versions")
    op.drop_index("uq_skills_key", table_name="skills")
    op.drop_table("skills")
