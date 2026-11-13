# SSE 帧与 seq 分配的确定性单测。
#
# 这些逻辑已从 chat 路由抽到 src/api/routers/sse.py，因此本文件不需要
# 拉起整条 agent / LLM 依赖链就能直接测试。

import json

import pytest

from src.api.routers.sse import SseWriter, error_event, sse_frame


def _data_line(frame: str) -> dict:
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"SSE 帧缺少 data 行: {frame!r}")


def test_frame_contains_only_data_when_no_seq():
    frame = sse_frame({"type": "token", "content": "你好"})
    assert frame == 'data: {"type": "token", "content": "你好"}\n\n'
    assert "id:" not in frame


def test_frame_emits_id_line_before_data_when_seq_given():
    frame = sse_frame({"type": "token", "content": "你好"}, seq=7)
    assert frame.startswith("id: 7\n")
    assert frame.endswith('data: {"type": "token", "content": "你好"}\n\n')
    assert _data_line(frame) == {"type": "token", "content": "你好"}


def test_frame_keeps_chinese_readable():
    # ensure_ascii=False：不要把中文转义成 \uXXXX，便于日志与排查
    assert "你好" in sse_frame({"content": "你好"})


def test_error_event_shape():
    assert error_event("busy", "忙") == {
        "type": "error", "code": "busy", "message": "忙", "retryable": True,
    }
    assert error_event("dup", "重", retryable=False)["retryable"] is False


def test_writer_counts_up_from_start(counting_redis):
    writer = SseWriter(counting_redis, "k", 10)
    assert [writer.take() for _ in range(3)] == [11, 12, 13]


def test_writer_resumes_from_persisted_value(counting_redis):
    writer = SseWriter(counting_redis, "k", 40)
    assert writer.take() == 41


def test_writer_frame_consumes_a_seq(counting_redis):
    writer = SseWriter(counting_redis, "k", 0)
    frame = writer.frame({"type": "token", "content": "a"})
    assert frame.startswith("id: 1\n")
    assert writer.take() == 2


def test_writer_frame_without_id_does_not_consume_seq(counting_redis):
    writer = SseWriter(counting_redis, "k", 0)
    assert "id:" not in writer.frame({"type": "ping"}, with_id=False)
    assert writer.take() == 1


async def test_writer_syncs_final_value(counting_redis):
    writer = SseWriter(counting_redis, "k", 0)
    writer.take()
    writer.take()
    await writer.sync()
    assert await counting_redis.get("k") == "2"


async def test_streaming_a_reply_does_not_touch_redis_per_token(counting_redis):
    # 200 个 token 若逐条 INCR 就是 200 次 Redis 往返
    writer = SseWriter(counting_redis, "k", 0)
    for _ in range(200):
        writer.take()
    await writer.sync()

    assert counting_redis.reads == 0  # 分配过程中一次都不读 Redis
    assert counting_redis.writes == 1  # 结束时只回写一次
    assert await counting_redis.get("k") == "200"


async def test_writer_sync_failure_is_non_fatal():
    class BrokenRedis:
        async def set(self, key: str, value, **kwargs) -> None:
            raise RuntimeError("redis down")

    writer = SseWriter(BrokenRedis(), "k", 0)
    writer.take()
    await writer.sync()  # 不应抛出，主链路继续
