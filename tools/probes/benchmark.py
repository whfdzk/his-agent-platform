"""
性能压测脚本 - 完整版
测试更多接口，找出性能瓶颈
"""

import asyncio
import time
import aiohttp
from statistics import mean, median


BASE_URL = "http://127.0.0.1:8082"
CONCURRENCY = 10
REQUESTS_PER_ENDPOINT = 10


# 不需要认证的接口
PUBLIC_ENDPOINTS = [
    ("GET", "/health", "健康检查"),
    ("GET", "/metrics", "Prometheus指标"),
    ("GET", "/api/v1/guide/hospitals", "医院列表"),
    ("GET", "/api/v1/guide/departments", "科室列表"),
    ("GET", "/api/v1/guide/departments/1", "科室详情"),
    ("GET", "/api/v1/safety/rules", "安全规则"),
    ("GET", "/api/v1/safety/limits", "安全限制"),
    ("GET", "/api/v1/model-configs", "模型配置"),
]


# 需要认证的接口（先登录获取 token）
AUTH_ENDPOINTS = [
    ("GET", "/api/v1/auth/me", "当前用户信息"),
    ("GET", "/api/v1/sessions", "会话列表"),
    ("GET", "/api/v1/knowledge/docs", "知识库文档"),
    ("GET", "/api/v1/knowledge/feedback/stats", "知识库反馈统计"),
    ("GET", "/api/v1/stats/tokens", "Token统计"),
    ("GET", "/api/v1/stats/consultations", "问诊统计"),
    ("GET", "/api/v1/safety/events", "安全事件"),
    ("GET", "/api/v1/safety/stats", "安全统计"),
    ("GET", "/api/v1/observability/stats", "可观测性统计"),
    ("GET", "/api/v1/admin/stats/overview", "管理端总览"),
    ("GET", "/api/v1/admin/conversations", "管理端会话列表"),
]


async def login(session):
    """登录获取 token"""
    # 尝试不同的登录接口
    for login_url in [
        "/api/v1/auth/login",
        "/api/v1/admin/auth/login",
    ]:
        try:
            async with session.post(
                f"{BASE_URL}{login_url}",
                json={"username": "admin", "password": "admin123"},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("access_token") or data.get("token")
                    if token:
                        print(f"  ✓ 登录成功 ({login_url})")
                        return token
        except Exception:
            pass
    print("  ✗ 登录失败，将跳过需要认证的接口")
    return None


async def benchmark_endpoint(session, method, path, name, token=None):
    """压测单个接口"""
    url = BASE_URL + path
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    latencies = []
    errors = 0
    status_codes = {}

    async def one_request():
        nonlocal errors
        start = time.perf_counter()
        try:
            if method == "GET":
                async with session.get(url, headers=headers) as resp:
                    await resp.read()
                    latency = (time.perf_counter() - start) * 1000
                    status = resp.status
                    return latency, status
        except Exception as e:
            errors += 1
            print(f"      错误: {type(e).__name__}: {e}")
            return None, None

    tasks = [one_request() for _ in range(REQUESTS_PER_ENDPOINT)]
    results = await asyncio.gather(*tasks)

    latencies = [r[0] for r in results if r[0] is not None]
    for r in results:
        if r[1]:
            status_codes[r[1]] = status_codes.get(r[1], 0) + 1

    if not latencies:
        return {
            "name": name,
            "path": path,
            "error": "全部失败",
            "errors": errors,
            "status_codes": status_codes,
        }

    avg_latency = mean(latencies)
    p50 = median(latencies)
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]
    p99 = sorted(latencies)[int(len(latencies) * 0.99)]

    rps = CONCURRENCY / (avg_latency / 1000)

    return {
        "name": name,
        "path": path,
        "requests": REQUESTS_PER_ENDPOINT,
        "errors": errors,
        "avg_ms": round(avg_latency, 1),
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
        "p99_ms": round(p99, 1),
        "rps": round(rps, 1),
        "status_codes": status_codes,
    }


async def main():
    print("=" * 100)
    print(f"性能压测报告")
    print(f"并发数: {CONCURRENCY}, 每个接口请求数: {REQUESTS_PER_ENDPOINT}")
    print("=" * 100)

    timeout = aiohttp.ClientTimeout(total=30)
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # 先登录
        print("\n=== 登录 ===")
        token = await login(session)

        # 测试公开接口
        print("\n=== 测试公开接口 ===")
        public_results = []
        for method, path, name in PUBLIC_ENDPOINTS:
            print(f"  测试: {name} ({path})...")
            result = await benchmark_endpoint(session, method, path, name)
            public_results.append(result)

        # 测试需要认证的接口
        auth_results = []
        if token:
            print("\n=== 测试认证接口 ===")
            for method, path, name in AUTH_ENDPOINTS:
                print(f"  测试: {name} ({path})...")
                result = await benchmark_endpoint(session, method, path, name, token)
                auth_results.append(result)

    # 合并结果
    all_results = public_results + auth_results

    # 打印报告
    print("\n" + "=" * 100)
    print("📊 性能压测结果")
    print("=" * 100)
    print(f"{'接口':<25} {'路径':<45} {'RPS':>8} {'P95(ms)':>10} {'P99(ms)':>10} {'错误':>6}")
    print("-" * 100)

    for r in all_results:
        if "error" in r:
            print(f"{r['name']:<25} {r['path']:<45} {'ERROR':>8} {'-':>10} {'-':>10} {r['errors']:>6}")
        else:
            print(f"{r['name']:<25} {r['path']:<45} {r['rps']:>8.1f} {r['p95_ms']:>10.1f} {r['p99_ms']:>10.1f} {r['errors']:>6}")

    # 找出瓶颈
    print("\n" + "=" * 100)
    print("🐌 性能瓶颈分析（按 P95 延迟从高到低排序）")
    print("=" * 100)

    valid_results = [r for r in all_results if "error" not in r]
    valid_results.sort(key=lambda x: x["p95_ms"], reverse=True)

    for i, r in enumerate(valid_results, 1):
        bar = "█" * int(r["p95_ms"] / 50)
        print(f"  {i:2d}. {r['name']:<20} P95={r['p95_ms']:>7.1f}ms  RPS={r['rps']:>6.1f}  {bar}")

    # 分类统计
    print("\n" + "=" * 100)
    print("📈 分类统计")
    print("=" * 100)

    # 快接口（P95 < 100ms）
    fast = [r for r in valid_results if r["p95_ms"] < 100]
    print(f"\n✅ 快接口 (P95 < 100ms): {len(fast)} 个")
    for r in fast:
        print(f"     - {r['name']}: P95={r['p95_ms']}ms, RPS={r['rps']}")

    # 中等接口（P95 100-500ms）
    medium = [r for r in valid_results if 100 <= r["p95_ms"] < 500]
    print(f"\n⚠️  中等接口 (P95 100-500ms): {len(medium)} 个")
    for r in medium:
        print(f"     - {r['name']}: P95={r['p95_ms']}ms, RPS={r['rps']}")

    # 慢接口（P95 > 500ms）
    slow = [r for r in valid_results if r["p95_ms"] >= 500]
    print(f"\n🐌 慢接口 (P95 >= 500ms): {len(slow)} 个")
    for r in slow:
        print(f"     - {r['name']}: P95={r['p95_ms']}ms, RPS={r['rps']}")


if __name__ == "__main__":
    asyncio.run(main())
