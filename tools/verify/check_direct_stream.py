import json
import sys
import time

import httpx

sys.stdout.reconfigure(encoding="utf-8")

Q = sys.argv[1] if len(sys.argv) > 1 else "阿司匹林有什么常见副作用"
SID = "ssetest-" + str(int(time.time()))

payload = {"session_id": SID, "message": Q}
t0 = time.perf_counter()
first = None
acc = []
tokens = 0
chars = 0
types = {}

with httpx.Client(timeout=200.0) as client:
    with client.stream("POST", "http://127.0.0.1:8000/api/v1/chat/stream", json=payload) as r:
        print("status:", r.status_code)
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            try:
                evt = json.loads(line[5:].strip())
            except Exception:
                continue
            t = evt.get("type")
            types[t] = types.get(t, 0) + 1
            if t == "token":
                c = evt.get("content") or ""
                tokens += 1
                chars += len(c)
                acc.append(c)
                if first is None:
                    first = time.perf_counter() - t0
            if t == "done":
                break

total = time.perf_counter() - t0
print(f"Q={Q} sid={SID}")
print(f"first_token={first:.1f}s tokens={tokens} chars={chars} total={total:.1f}s types={types}")
print("TEXT>>>", repr("".join(acc)[:600]))
