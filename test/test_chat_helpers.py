# 聊天路由中不依赖外部服务的纯逻辑单测：
# 身份解析、异常转译、幂等认领、游客模式不落库。
# 这里刻意只测可确定性验证的部分，全链路仍需 -m integration 的集成用例。
# SSE 帧格式与 seq 分配已随 sse.py 抽出，见 test_sse.py。

import asyncio
import json
import types

import pytest

from src.api.routers.chat import (
    ChatIdentity,
    _friendly_error,
    _idem_check_and_claim,
    _persist_message,
    _resolve_identity,
    _thread_id,
)
from src.core.concurrency import SystemBusyError


def _request(host: str = "1.2.3.4"):
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host))


def _ctx(subject_id: str, tenant_id: int = 1, role: str = "patient"):
    """轻量替身：_resolve_identity 只读 subject_id / tenant_id / role。"""
    return types.SimpleNamespace(subject_id=subject_id, tenant_id=tenant_id, role=role)


def _identity(user_id: str, patient_id: int | None = None) -> ChatIdentity:
    return ChatIdentity(
        user_id=user_id,
        authenticated=user_id != "anon",
        rate_key=f"user:{user_id}" if user_id != "anon" else "ip:1.2.3.4",
        patient_id=patient_id,
        tenant_id=1,
    )


# ── 异常 → 用户友好事件（绝不外泄堆栈）────────────────────────────────────
def test_friendly_error_maps_system_busy():
    assert _friendly_error(SystemBusyError("饱和"))["code"] == "busy"


def test_friendly_error_maps_timeout():
    assert _friendly_error(asyncio.TimeoutError())["code"] == "timeout"


def test_friendly_error_falls_back_to_internal():
    assert _friendly_error(RuntimeError("boom"))["code"] == "internal"


def test_friendly_error_never_leaks_internal_detail():
    payload = json.dumps(_friendly_error(RuntimeError("内部机密")), ensure_ascii=False)
    assert "内部机密" not in payload
    assert "Traceback" not in payload


# ── 身份解析 ──────────────────────────────────────────────────────────────
def test_authenticated_identity_persists_and_uses_user_rate_key():
    identity = _resolve_identity(_ctx("42"), _request())
    assert identity.user_id == "42"
    assert identity.authenticated
    assert identity.persist
    assert identity.patient_id == 42
    assert identity.rate_key == "user:42"


def test_anonymous_identity_never_persists_and_uses_ip_rate_key():
    identity = _resolve_identity(None, _request())
    assert identity.user_id == "anon"
    assert not identity.authenticated
    assert not identity.persist
    assert identity.patient_id is None
    assert identity.rate_key == "ip:1.2.3.4"


def test_non_numeric_user_id_keeps_authenticated_without_patient_id():
    identity = _resolve_identity(_ctx("not-a-number"), _request())
    assert identity.authenticated
    assert identity.persist
    assert identity.patient_id is None


def test_thread_id_isolates_sessions_and_separates_anon_from_users():
    anon = _resolve_identity(None, _request())
    user = _resolve_identity(_ctx("7"), _request())
    assert _thread_id(anon, "s1") == "anon:s1"
    assert _thread_id(anon, "s2") == "anon:s2"
    assert _thread_id(user, "s1") == "7:s1"


# ── 幂等认领 ──────────────────────────────────────────────────────────────
async def test_idem_disabled_without_client_message_id(redis):
    assert await _idem_check_and_claim(redis, "u1", None) == "claimed"


async def test_idem_first_call_claims(redis):
    assert await _idem_check_and_claim(redis, "u1", "cm1") == "claimed"


async def test_idem_second_call_reports_in_flight(redis):
    await _idem_check_and_claim(redis, "u1", "cm1")
    assert await _idem_check_and_claim(redis, "u1", "cm1") == "processing"


async def test_idem_replays_stored_result(redis):
    await _idem_check_and_claim(redis, "u1", "cm1")
    await redis.set("idem:u1:cm1", json.dumps({"reply": "你好", "appointment": None}))
    stored = await _idem_check_and_claim(redis, "u1", "cm1")
    assert json.loads(stored)["reply"] == "你好"


async def test_idem_reports_in_flight_with_bytes_mode_client(bytes_redis):
    # 聊天路由用的是 bytes 模式的 Redis 客户端，不解码会让 "processing" 判断恒不成立，
    # 进而把 b"processing" 当成存量回复喂给 json.loads 直接 500
    await _idem_check_and_claim(bytes_redis, "u1", "cm1")
    assert await _idem_check_and_claim(bytes_redis, "u1", "cm1") == "processing"


async def test_idem_replays_stored_result_with_bytes_mode_client(bytes_redis):
    await _idem_check_and_claim(bytes_redis, "u1", "cm1")
    await bytes_redis.set("idem:u1:cm1", json.dumps({"reply": "你好", "appointment": None}))
    stored = await _idem_check_and_claim(bytes_redis, "u1", "cm1")
    assert json.loads(stored)["reply"] == "你好"


# ── 游客模式不落库 ────────────────────────────────────────────────────────
async def test_anonymous_messages_only_bump_seq_without_db(redis):
    seq1 = await _persist_message(redis, _identity("anon"), "s1", role="user", content="你好", persist=False)
    seq2 = await _persist_message(redis, _identity("anon"), "s1", role="assistant", content="嗯", persist=False)
    assert (seq1, seq2) == (1, 2)
    # 游客不应产生任何 conversation 缓存（落库的前置步骤）
    assert await redis.exists("chat:conv:anon:s1") == 0


async def test_persist_failure_does_not_raise(redis):
    # 下游异常被吞掉并记日志，主链路不受影响
    class BrokenRedis:
        async def incr(self, key: str) -> int:
            raise RuntimeError("redis down")

    assert await _persist_message(BrokenRedis(), _identity("u1"), "s1", role="user", content="x") is None



