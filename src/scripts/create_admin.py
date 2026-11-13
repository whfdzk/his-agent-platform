"""创建/重置后台管理员账号（离线脚本，不提供公开的注册接口）。

用法：
    python scripts/create_admin.py admin admin123
    python scripts/create_admin.py admin admin123 --name 平台管理员
    python scripts/create_admin.py admin admin123 --reset   # 已存在时重置密码/启用

账号表由 alembic 迁移创建，执行前先 `alembic upgrade head`。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import select

from src.infra.database import AsyncSessionLocal
from src.modules.admin.model import ROLE_ADMIN, Admin
from src.utils.password_utils import hash_password


async def _upsert(username: str, password: str, display_name: str, reset: bool) -> int:
    async with AsyncSessionLocal() as db:
        admin = (
            await db.execute(select(Admin).where(Admin.username == username).limit(1))
        ).scalar_one_or_none()

        if admin is not None:
            if not reset:
                print(f"管理员「{username}」已存在（id={admin.id}）；加 --reset 可重置密码")
                return 1
            admin.password_hash = hash_password(password)
            admin.display_name = display_name or admin.display_name
            admin.enabled = True
            await db.commit()
            print(f"已重置管理员「{username}」的密码（id={admin.id}）")
            return 0

        db.add(Admin(
            username=username,
            password_hash=hash_password(password),
            display_name=display_name or username,
            role=ROLE_ADMIN,
            enabled=True,
        ))
        await db.commit()
        print(f"已创建管理员「{username}」")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="创建后台管理员账号")
    ap.add_argument("username", help="登录账号")
    ap.add_argument("password", help="登录密码（不少于 6 位）")
    ap.add_argument("--name", default="", help="展示名称")
    ap.add_argument("--reset", action="store_true", help="账号已存在时重置密码并启用")
    args = ap.parse_args()

    if len(args.password) < 6:
        print("密码至少 6 位", file=sys.stderr)
        return 1

    return asyncio.run(_upsert(args.username, args.password, args.name, args.reset))


if __name__ == "__main__":
    raise SystemExit(main())