import { useEffect, useRef, useState, useCallback } from 'react';

/**
 * 轮询加载器（C4 的实时性先用 10s 轮询，SSE 留给 C3 的接管通道）。
 *
 * 三个必须处理的细节：
 *  - 页面切到后台不轮询（医生切走标签页还在打接口，纯浪费）；
 *  - 上一次未返回不发下一次，用序号丢弃过期响应（避免旧数据覆盖新数据）；
 *  - 404/403 等业务错误只记错误文案，不清数据 —— 医生不该因为一次失败看不到已加载的列表。
 */
export function usePolling<T>(
  loader: () => Promise<T>,
  intervalMs = 10_000,
): {
  data: T | null;
  error: string;
  loading: boolean;
  updatedAt: number | null;
  refresh: () => Promise<void>;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);

  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const seq = useRef(0);

  const tick = useCallback(async (force = false) => {
    // 后台标签页跳过轮询，但首屏那次必须拉：医生在后台标签页打开工作台时，
    // 若连首次请求都被 visibilityState 拦掉，页面就会一直停在"加载中"，
    // 直到用户切回来才出数据 —— 那是把"省流量"写成了"打不开"。
    if (!force && typeof document !== 'undefined' && document.hidden) return;
    const mine = ++seq.current;
    setLoading(true);
    try {
      const result = await loaderRef.current();
      if (mine !== seq.current) return; // 已有更新的请求在跑，丢弃本次
      setData(result);
      setError('');
      setUpdatedAt(Date.now());
    } catch (e) {
      if (mine !== seq.current) return;
      setError((e as Error).message || '加载失败');
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void tick(true);
    const id = window.setInterval(() => void tick(), intervalMs);
    const onVisible = () => {
      if (!document.hidden) void tick(true);
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(id);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [tick, intervalMs]);

  const refresh = useCallback(() => tick(true), [tick]);

  return { data, error, loading, updatedAt, refresh };
}
