"""授予 / 回收账号角色（离线脚本，不开放 HTTP 入口）。

为什么不走接口：
  角色授予是提权操作，暴露成 API 就等于给了一条"只要能登管理员就能自提 superadmin"的路。
  先做成离线脚本，等 B3 医护端落地、审计链路齐了再考虑加接口 + 二次确认。

用法：
    # 看有哪些角色可授
    python -m src.scripts.grant_role --roles

    # 给患者账号 4 授予"医生"角色（医护侧：可看患者档案、审批高危动作）
    python -m src.scripts.grant_role --kind patient --id 4 --role clinician --note "心内科"

    # 回收
    python -m src.scripts.grant_role --kind patient --id 4 --role clinician --revoke

    # 查看某账号现有授权
    python -m src.scripts.grant_role --kind patient --id 4 --list

注意：改完角色不影响已签发的 token（JWT 内 scopes 是签发时快照）。
      被授权人需重新登录；紧急撤权用其账号"登出全部设备"。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loguru import logger

from src.core.permissions import ROLE_DEFS, scopes_for_roles
from src.infra.database import AsyncSessionLocal
from src.modules.rbac import service as rbac


def _print_roles() -> None:
    print("可授予角色：")
    for code, spec in ROLE_DEFS.items():
        scopes = ", ".join(sorted(scopes_for_roles([code]))) or "-"
        print(f"  {code:<12} {spec['name']:<6} side={spec['side']:<8} {spec['description']}")
        print(f"  {'':<12} scopes: {scopes}")


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as db:
        if args.list:
            grants = await rbac.list_grants(db, args.kind, args.id)
            if not grants:
                print(f"{args.kind}#{args.id} 没有任何角色记录（登录时按默认角色兜底）")
                return 0
            print(f"{args.kind}#{args.id} 的角色：")
            for g in grants:
                print(f"  - {g['role_code']}  by={g['granted_by'] or '-'}  note={g['note'] or '-'}  at={g['created_at']}")
            return 0

        if args.revoke:
            removed = await rbac.revoke_role(db, args.kind, args.id, args.role)
            print(f"回收 {'成功' if removed else '未找到该授权'}：{args.kind}#{args.id} role={args.role}")
            if not removed:
                return 1
        else:
            added = await rbac.grant_role(
                db, args.kind, args.id, args.role,
                granted_by=getattr(args, "by", None) or "cli", note=args.note or "",
            )
            print(f"{'授予成功' if added else '已存在该角色，无需重复授予'}：{args.kind}#{args.id} role={args.role}")

        roles, role, scopes = await rbac.identity_of(db, args.kind, args.id)
        print(f"当前生效：roles={list(roles)} 主角色={role}")
        print(f"scopes={sorted(scopes)}")
        print("提示：已登录的 token 仍持有旧 scopes，需重新登录后生效")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="授予 / 回收 RBAC 角色")
    parser.add_argument("--roles", action="store_true", help="列出所有可授予角色及其权限")
    parser.add_argument("--kind", choices=["patient", "admin"], help="主体类型：patient / admin")
    parser.add_argument("--id", type=int, help="主体 ID（patients.id 或 admins.id）")
    parser.add_argument("--role", help="角色码，如 clinician / nurse / superadmin")
    parser.add_argument("--revoke", action="store_true", help="回收该角色")
    parser.add_argument("--list", action="store_true", help="列出该主体当前角色")
    parser.add_argument("--note", default="", help="授予备注（记入审计）")
    parser.add_argument("--by", default="cli", help="授予人标识")
    args = parser.parse_args()

    if args.roles:
        _print_roles()
        return 0

    if not args.kind or args.id is None:
        parser.error("需要 --kind 与 --id（或用 --roles 查看角色清单）")
    if not args.list and not args.role:
        parser.error("需要 --role（或用 --list 查看现有授权）")

    try:
        return asyncio.run(_run(args))
    except rbac.RbacError as e:
        logger.error(f"授予失败：{e}")
        print(f"失败：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
