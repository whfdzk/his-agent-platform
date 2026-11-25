# src/api/routers/admin_harness.py
#
# 管理端：Harness 层的自述与运行时观测。
#
# 面试/评审最常追问的三件事，这里一屏能看到：
#   1. 各层职责落在哪个文件（layers）
#   2. 工具受控执行的计数（调用/失败/超时/重试/拒绝/平均耗时）
#   3. 当前生效的 Guardrail 策略（黑名单、参数上限）

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.deps import get_current_admin
from src.harness.core import get_harness

router = APIRouter(
    prefix="/api/v1/admin/harness",
    tags=["admin-harness"],
    dependencies=[Depends(get_current_admin)],
)


@router.get("/overview")
async def harness_overview():
    return get_harness().overview()


@router.get("/runtime")
async def harness_runtime():
    return get_harness().runtime_stats()
