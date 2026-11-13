# ============================================================
# 就医辅助数据初始化脚本
#
# 1) 科室介绍：departments.description（医疗知识库里只有科室名，没有简介）
# 2) 医院信息：hospitals（机构信息属外部公示数据，不内置写死，
#    改为从可选的 scripts/hospitals.json 导入，避免把未经核对的地址电话写进库）
#
# 前置条件：alembic upgrade head（含 c1d9e4b7a305）
# 用法: cd his-agent-platform && python src/scripts/init_guide_data.py
# ============================================================

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HOSPITALS_JSON = Path(__file__).resolve().parent / "hospitals.json"

# 科室简介：诊疗范围口径，供 GuideAgent 的"科室介绍查询"使用。
# 只写通用医学共识，不写具体医院的号源/专家信息。
DEPARTMENT_INTRO: dict[str, str] = {
    "内科": "诊治成人非手术类疾病的总称，涵盖呼吸、消化、心血管、内分泌等系统的常见病与慢性病管理，是多数首发症状的首诊科室。",
    "外科": "以手术治疗为主的科室，处理创伤、感染、肿瘤、畸形等需要手术干预的疾病，同時负责术前评估与术后随访。",
    "心内科": "诊治高血压、冠心病、心律失常、心力衰竭等心血管疾病，开展心电图、心脏超声、冠脉造影等检查与长期用药管理。",
    "呼吸内科": "诊治咳嗽、哮喘、慢阻肺、肺部感染等疾病，开展肺功能检查与呼吸系统慢性病的长期管理。",
    "消化内科": "诊治胃炎、溃疡、肝病、胰腺疾病及消化道出血等，开展胃肠镜等内镜诊疗。",
    "内分泌科": "诊治糖尿病、甲状腺疾病、骨质疏松、血脂异常等代谢与内分泌疾病，负责长期指标监测与用药调整。",
    "神经内科": "诊治头痛、头晕、脑卒中、癫痫、帕金森病等神经系统疾病，开展头颅影像与电生理检查评估。",
    "肾内科": "诊治肾炎、肾功能不全、尿毒症等肾脏疾病，负责透析与慢性肾病管理。",
    "血液科": "诊治贫血、白血病、出血与凝血障碍等血液系统疾病。",
    "风湿免疫科": "诊治类风湿关节炎、痛风、系统性红斑狼疮等风湿免疫性疾病。",
    "骨科": "诊治骨折、关节损伤、颈腰椎病、运动损伤等骨与关节疾病，开展复位固定与关节置换等手术。",
    "妇产科": "负责妇科疾病诊疗、孕前检查、孕期保健与分娩，开展妇科超声与宫颈筛查。",
    "儿科": "诊治 0-14 岁儿童各系统疾病，负责生长发育评估、预防接种指导与儿童用药剂量调整。",
    "皮肤科": "诊治湿疹、荨麻疹、银屑病、痤疮、真菌感染等皮肤疾病及性传播疾病筛查。",
    "眼科": "诊治视力下降、白内障、青光眼、眼底病变等眼部疾病，开展验光与眼压、眼底检查。",
    "耳鼻喉科": "诊治鼻炎、咽喉炎、扁桃体炎、中耳炎、耳鸣与听力下降等耳鼻咽喉疾病。",
    "口腔科": "诊治龋齿、牙周病、口腔黏膜病，开展拔牙、修复与正畸治疗。",
    "精神心理科": "诊治焦虑、抑郁、失眠、双相障碍等精神心理问题，提供心理评估与药物治疗。",
    "急诊科": "处理突发外伤、急性胸痛、呼吸困难、中毒、大出血等急危重症，24 小时开放，优先救治危及生命的情况。",
    "中医科": "以中医辨证论治为主，运用中药、针灸、推拿等方式调理慢性病、亚健康与康复期状态。",
    "康复医学科": "负责脑卒中、骨折术后、运动损伤等导致的功能障碍康复训练与评估。",
    "全科医学科": "负责常见病首诊、慢性病随访、健康咨询与转诊，适合症状不典型、尚不确定专科方向的患者。",
    "体检中心": "提供健康体检套餐、检验影像检查与体检报告解读，不负责疾病治疗。",
}


def import_departments(cur) -> int:
    updated = 0
    for name, intro in DEPARTMENT_INTRO.items():
        cur.execute(
            "UPDATE departments SET description = %s WHERE name = %s AND (description IS NULL OR description = '')",
            (intro, name),
        )
        updated += cur.rowcount
    return updated


def import_hospitals(cur, records: list[dict]) -> int:
    inserted = 0
    for item in records:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        cur.execute("SELECT id FROM hospitals WHERE name = %s", (name,))
        if cur.fetchone():
            continue
        cur.execute(
            """
            INSERT INTO hospitals
            (name, level, hospital_type, province, city, address, phone,
             emergency_phone, open_hours, intro, departments)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                name,
                item.get("level"),
                item.get("hospital_type"),
                item.get("province"),
                item.get("city"),
                item.get("address"),
                item.get("phone"),
                item.get("emergency_phone"),
                item.get("open_hours"),
                item.get("intro"),
                item.get("departments"),
            ),
        )
        inserted += 1
    return inserted


def main() -> None:
    import psycopg2

    settings = get_settings()
    conn = psycopg2.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        dbname=settings.DB_NAME,
    )
    conn.autocommit = False
    cur = conn.cursor()

    dept_count = import_departments(cur)
    conn.commit()
    logger.info(f"科室简介补充完成：{dept_count} 个")

    if not HOSPITALS_JSON.exists():
        logger.warning(
            f"未找到 {HOSPITALS_JSON.name}，跳过医院导入。"
            "如需医院信息查询，请按 hospitals.json.example 的格式放入经核对的机构公示数据。"
        )
    else:
        records = json.loads(HOSPITALS_JSON.read_text(encoding="utf-8"))
        count = import_hospitals(cur, records if isinstance(records, list) else [])
        conn.commit()
        logger.info(f"医院信息导入完成：{count} 家")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
