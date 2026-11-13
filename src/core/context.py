# src/core/context.py
#
# 跨层共享的运行时上下文。
#
# UserContext 被 api / agents / tools 三层共用，放在 core（谁都依赖、不依赖
# 任何人）可以避免 tools 为了拿这个 dataclass 而反向依赖 agents 层。

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class UserContext:  # 用户上下文
    user_id: str
    session_id: str
    # False=游客（未登录）：可以聊天，但不落库、不写长期记忆、不能新建会话
    authenticated: bool = True
    # 登录用户对应的患者档案 ID（= user_id 转 int），游客为 None
    patient_id: int | None = None
    # 角色：patient=患者端（默认）；doctor/pharmacist/staff/admin 才能触达运营库（NL2SQL）
    role: str = "patient"
