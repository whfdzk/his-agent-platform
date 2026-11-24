"""API 层端到端冒烟：真实后端 + 真实中间件，不 mock。

覆盖链路：
  健康检查 → 患者登录 → 身份校验（正向/负向）→ 会话列表 → SSE 问诊一轮
  → 历史回放 → 预约列表 → 工单创建/查询 → 患者概览 → 越权断面

用法：
  python tools/verify/e2e_api_smoke.py [--base http://127.0.0.1:8000] [--no-chat]

--no-chat 跳过会调用大模型的 SSE 环节（用于快速回归）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PHONE = os.getenv("EVAL_PHONE", "13800138001")
PASSWORD = os.getenv("EVAL_PASSWORD", "test123456")


def _pct(ms: float) -> str:
    return f"{ms:.0f}ms"


class Runner:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(base_url=self.base, timeout=30.0, trust_env=False)
        self.results: list[dict] = []
        self.token = ""
        self.session_id = f"e2e-{int(time.time())}"

    def check(self, name: str, fn) -> bool:
        t0 = time.perf_counter()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本要把异常转成失败项
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - t0) * 1000
        self.results.append({"case": name, "ok": ok, "detail": detail, "elapsed_ms": round(ms, 1)})
        print(f"{'PASS' if ok else 'FAIL'}  {name:<38} {detail}  ({_pct(ms)})")
        return ok

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ── 用例 ────────────────────────────────────────────────────────────
    def health(self):
        r = self.client.get("/health")
        return r.status_code == 200 and r.json().get("status") == "ok", f"HTTP {r.status_code}"

    def login(self):
        r = self.client.post("/api/v1/auth/login", json={"phone": PHONE, "password": PASSWORD})
        if r.status_code != 200:
            return False, f"HTTP {r.status_code} {r.text[:120]}"
        body = r.json()
        self.token = body.get("token") or ""
        return bool(self.token), f"user={body.get('user', {}).get('name', '?')}"

    def me_requires_token(self):
        r = self.client.get("/api/v1/auth/me")
        return r.status_code == 401, f"HTTP {r.status_code}"

    def me_with_token(self):
        r = self.client.get("/api/v1/auth/me", headers=self._auth())
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        name = ((r.json() or {}).get("user") or {}).get("name")
        return bool(name), f"user={name}"

    def patient_token_blocked_on_admin(self):
        r = self.client.get("/api/v1/admin/auth/me", headers=self._auth())
        return r.status_code == 403, f"HTTP {r.status_code}"

    def sessions_list(self):
        r = self.client.get("/api/v1/sessions", headers=self._auth())
        return r.status_code == 200 and isinstance(r.json(), (list, dict)), f"HTTP {r.status_code}"

    def appointments_list(self):
        r = self.client.get("/api/v1/appointments", headers=self._auth())
        return r.status_code == 200, f"HTTP {r.status_code}"

    def appointments_require_token(self):
        r = self.client.get("/api/v1/appointments")
        return r.status_code in (401, 403), f"HTTP {r.status_code}"

    def patient_overview(self):
        r = self.client.get("/api/v1/patient/overview", headers=self._auth())
        return r.status_code == 200, f"HTTP {r.status_code}"

    def create_ticket(self):
        r = self.client.post(
            "/api/v1/handoff/tickets",
            headers=self._auth(),
            json={"session_id": self.session_id, "summary": "E2E 联调工单", "priority": "normal"},
        )
        if r.status_code not in (200, 201):
            return False, f"HTTP {r.status_code} {r.text[:120]}"
        body = r.json() or {}
        ticket = body.get("ticket") or body
        self.ticket_id = str(ticket.get("id") or ticket.get("ticket_id") or "")
        return bool(self.ticket_id), f"ticket={self.ticket_id} no={ticket.get('ticket_no', '')}"

    def get_ticket(self):
        if not getattr(self, "ticket_id", ""):
            return False, "无 ticket_id（上一步失败）"
        r = self.client.get(f"/api/v1/handoff/tickets/{self.ticket_id}", headers=self._auth())
        return r.status_code == 200, f"HTTP {r.status_code}"

    def chat_stream_once(self):
        payload = {"session_id": self.session_id, "message": "你好"}
        deadline = time.time() + 200
        first_byte_ms = None
        events: dict[str, int] = {}
        t0 = time.perf_counter()
        with self.client.stream(
            "POST", "/api/v1/chat/stream", headers=self._auth(), json=payload, timeout=210.0,
        ) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            for line in resp.iter_lines():
                if time.time() > deadline:
                    break
                if first_byte_ms is None:
                    first_byte_ms = (time.perf_counter() - t0) * 1000
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw in ("[DONE]", "done"):
                    events["done"] = events.get("done", 0) + 1
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    events["raw"] = events.get("raw", 0) + 1
                    continue
                events[str(obj.get("type", "unknown"))] = events.get(str(obj.get("type", "unknown")), 0) + 1
        ok = events.get("done", 0) > 0 or sum(events.values()) > 0
        detail = f"first={_pct(first_byte_ms or 0)} events={events}"
        return ok, detail

    def history_replay(self):
        r = self.client.get(f"/api/v1/sessions/{self.session_id}/messages", headers=self._auth())
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        data = r.json()
        items = data if isinstance(data, list) else data.get("items") or data.get("messages") or []
        return len(items) >= 1, f"messages={len(items)}"

    # ── 编排 ────────────────────────────────────────────────────────────
    def run(self, with_chat: bool) -> int:
        print(f"BASE={self.base}  session={self.session_id}\n")
        self.check("健康检查 /health", self.health)
        if not self.check("患者登录", self.login):
            print("\n登录失败，后续用例无法执行")
            return self._finish()
        self.check("/auth/me 无 token 必须 401", self.me_requires_token)
        self.check("/auth/me 带 token 返回身份", self.me_with_token)
        self.check("患者 token 打管理端必须 403", self.patient_token_blocked_on_admin)
        self.check("会话列表", self.sessions_list)
        self.check("预约列表", self.appointments_list)
        self.check("预约列表无 token 必须 401/403", self.appointments_require_token)
        self.check("患者概览", self.patient_overview)
        self.check("创建人工工单", self.create_ticket)
        self.check("查询工单", self.get_ticket)
        if with_chat:
            self.check("SSE 问诊一轮", self.chat_stream_once)
            self.check("会话历史回放", self.history_replay)
        else:
            print("SKIP  SSE 问诊一轮 / 历史回放（--no-chat）")
        return self._finish()

    def _finish(self) -> int:
        total = len(self.results)
        passed = sum(1 for r in self.results if r["ok"])
        out = ROOT / "doc" / "eval-reports" / f"e2e-api-{datetime.now():%Y%m%d}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "base": self.base,
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "passed": passed,
                    "total": total,
                    "rate": round(passed / total, 4) if total else 0.0,
                    "cases": self.results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n结果：{passed}/{total} 通过，报告 {out}")
        return 0 if passed == total else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.getenv("E2E_BASE", "http://127.0.0.1:8000"))
    ap.add_argument("--no-chat", action="store_true", help="跳过调用大模型的 SSE 环节")
    args = ap.parse_args()
    return Runner(args.base).run(with_chat=not args.no_chat)


if __name__ == "__main__":
    sys.exit(main())
