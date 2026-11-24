# ============================================================
# 补充常用药 + 说明书（幂等，可重复执行）
#
# 背景：drugs 表只有 8 条（布洛芬/阿莫西林/感冒灵/硝苯地平/二甲双胍/
# 奥美拉唑/氯雷他定/维生素C），评测里问"阿司匹林"时只能答"暂未收录"。
# 这里补最常见的心血管基础药，并把说明书章节写全（indication /
# usage_dosage / contraindication / precaution / adverse_reaction /
# interaction / full_instruction），medication_tools 与
# doc_ingestion.import_drug_instructions 都直接读这两张表。
#
# 用法: python -m src.scripts.seed_drug_extra
# ============================================================

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


ASPIRIN = {
    "name": "阿司匹林肠溶片",
    "alias": "阿司匹林、乙酰水杨酸、拜阿司匹灵",
    "category": "抗血小板药",
    "is_otc": False,
    "detail": {
        "ingredient": "本品主要成份为阿司匹林（乙酰水杨酸），辅料含淀粉、滑石粉、肠溶包衣材料。",
        "indication": (
            "1. 抑制血小板聚集：用于降低心肌梗死、脑卒中（缺血性）及短暂性脑缺血发作的复发风险，"
            "用于冠心病、缺血性脑血管病的二级预防，以及经医生评估后的高危人群一级预防。\n"
            "2. 解热镇痛：用于普通感冒或流行性感冒引起的发热，缓解轻至中度疼痛如头痛、牙痛、"
            "神经痛、肌肉痛、痛经。\n"
            "3. 抗炎抗风湿：用于风湿热、类风湿关节炎等（现多被其他药物替代）。"
        ),
        "usage_dosage": (
            "抗血小板治疗：常用 75～100 mg，每日 1 次，整片吞服，不可掰开、压碎或嚼服"
            "（肠溶衣被破坏会直接刺激胃黏膜）。\n"
            "解热镇痛：成人一次 0.3～0.6 g，一日 3 次，必要时服用；24 小时不超过 2 g，"
            "连续使用解热不超过 3 天、镇痛不超过 5 天。\n"
            "服药时间：肠溶片建议餐前空腹整片吞服，用足量水送服；具体剂量与疗程遵医嘱。"
        ),
        "contraindication": (
            "1. 对阿司匹林或其他水杨酸类过敏，或服用后出现哮喘、荨麻疹、血管性水肿者禁用；\n"
            "2. 活动性消化道溃疡、消化道出血或既往有溃疡出血史者禁用；\n"
            "3. 有出血倾向（血友病、血小板减少）、正在使用抗凝药且未评估者禁用；\n"
            "4. 严重肝肾功能不全、严重心力衰竭者禁用；\n"
            "5. 妊娠晚期（最后 3 个月）禁用；哺乳期慎用；\n"
            "6. 儿童及青少年在病毒感染（如水痘、流感）期间禁用，以免诱发瑞氏综合征。"
        ),
        "precaution": (
            "1. 长期服用者应定期监测大便隐血与血常规，出现黑便、呕血、牙龈或皮肤异常出血应立即停药就医；\n"
            "2. 择期手术、拔牙、胃肠镜活检前是否停药需由医生决定（通常提前 5～7 天评估），不可自行停用；\n"
            "3. 服药期间避免饮酒，减少胃肠道出血风险；\n"
            "4. 与布洛芬等 NSAIDs 合用会削弱抗血小板作用并增加出血，应避免自行叠加；\n"
            "5. 哮喘、鼻息肉、慢性胃病、痛风、葡萄糖-6-磷酸脱氢酶缺乏者慎用；\n"
            "6. 本品为处方药，是否适合长期服用（尤其一级预防）必须由医生按出血风险评估决定。"
        ),
        "adverse_reaction": (
            "常见：恶心、上腹不适、反酸、烧心；\n"
            "较重要：胃肠道溃疡与出血（可表现为黑便、贫血）、过敏反应（皮疹、支气管痉挛）、"
            "耳鸣与听力下降（水杨酸中毒早期表现）、肝肾功能异常、血小板功能抑制导致出血时间延长。"
        ),
        "interaction": (
            "与抗凝药（华法林、肝素）或抗血小板药（氯吡格雷、替格瑞洛）合用出血风险显著增加；\n"
            "与布洛芬、吲哚美辛等 NSAIDs 合用增加消化道损伤并竞争性削弱阿司匹林的抗血小板效应；\n"
            "与糖皮质激素合用增加溃疡出血风险；与利尿药、降压药合用可减弱其效果；\n"
            "与甲氨蝶呤合用增加其毒性；服药期间饮酒增加胃出血风险。"
        ),
        "storage": "遮光、密封，在干燥处保存；有效期见包装。",
        "full_instruction": (
            "【药品名称】通用名：阿司匹林肠溶片；英文名：Aspirin Enteric-coated Tablets。\n"
            "【成份】主要成份为阿司匹林（乙酰水杨酸）。\n"
            "【适应症】抑制血小板聚集，用于降低心肌梗死、缺血性卒中及短暂性脑缺血发作的复发风险，"
            "用于冠心病与缺血性脑血管病的二级预防；亦用于解热镇痛与抗炎抗风湿。\n"
            "【规格】100 mg/片（肠溶片）。\n"
            "【用法用量】抗血小板：一次 75～100 mg，一日 1 次，整片吞服，不可掰开或嚼碎。"
            "解热镇痛：成人一次 0.3～0.6 g，一日 3 次，24 小时不超过 2 g；"
            "解热连续不超过 3 天，镇痛不超过 5 天。请遵医嘱使用。\n"
            "【禁忌】对水杨酸类过敏者、活动性消化道溃疡或出血者、出血倾向者、"
            "严重肝肾功能不全者、妊娠晚期妇女、病毒感染期儿童及青少年禁用。\n"
            "【注意事项】长期服用需监测大便隐血；出现黑便、呕血或异常出血立即就医；"
            "手术或侵入性操作前停药需医生评估；避免饮酒；避免与布洛芬等 NSAIDs 自行合用；"
            "本品为处方药，长期服用须由医生按出血风险决定。\n"
            "【不良反应】胃肠道不适、溃疡与出血、过敏反应、耳鸣、肝肾功能异常、出血时间延长。\n"
            "【药物相互作用】与抗凝药、其他抗血小板药、糖皮质激素、甲氨蝶呤、利尿药、"
            "降压药、其他 NSAIDs 存在重要相互作用，合用前请咨询医生或药师。\n"
            "【贮藏】遮光、密封，干燥处保存。\n"
            "【说明】以上为通用说明书要点，具体用法以所购药品说明书及医生处方为准。"
        ),
    },
}


def main() -> None:
    import psycopg2

    settings = get_settings()
    conn = psycopg2.connect(
        host=settings.DB_HOST, port=settings.DB_PORT,
        user=settings.DB_USER, password=settings.DB_PASSWORD,
        dbname=settings.DB_NAME,
    )
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM drugs WHERE name = %s", (ASPIRIN["name"],))
        row = cur.fetchone()
        if row:
            drug_id = row[0]
            cur.execute(
                "UPDATE drugs SET alias=%s, category=%s, is_otc=%s WHERE id=%s",
                (ASPIRIN["alias"], ASPIRIN["category"], ASPIRIN["is_otc"], drug_id),
            )
            logger.info("药品已存在，更新元信息: id=%s", drug_id)
        else:
            cur.execute(
                "INSERT INTO drugs (name, alias, category, is_otc) VALUES (%s,%s,%s,%s) RETURNING id",
                (ASPIRIN["name"], ASPIRIN["alias"], ASPIRIN["category"], ASPIRIN["is_otc"]),
            )
            drug_id = cur.fetchone()[0]
            logger.info("新增药品: %s id=%s", ASPIRIN["name"], drug_id)

        d = ASPIRIN["detail"]
        cur.execute(
            """
            INSERT INTO drug_details (drug_id, ingredient, indication, usage_dosage,
                adverse_reaction, contraindication, precaution, interaction, storage, full_instruction)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id, drug_id) DO UPDATE SET
                ingredient = EXCLUDED.ingredient,
                indication = EXCLUDED.indication,
                usage_dosage = EXCLUDED.usage_dosage,
                adverse_reaction = EXCLUDED.adverse_reaction,
                contraindication = EXCLUDED.contraindication,
                precaution = EXCLUDED.precaution,
                interaction = EXCLUDED.interaction,
                storage = EXCLUDED.storage,
                full_instruction = EXCLUDED.full_instruction,
                updated_at = now()
            """,
            (drug_id, d["ingredient"], d["indication"], d["usage_dosage"],
             d["adverse_reaction"], d["contraindication"], d["precaution"],
             d["interaction"], d["storage"], d["full_instruction"]),
        )
        conn.commit()
        logger.info("说明书写入完成: drug_id=%s, full_instruction %s 字", drug_id, len(d["full_instruction"]))
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
