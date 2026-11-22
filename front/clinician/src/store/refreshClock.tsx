import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

/**
 * 顶栏「最近刷新」的统一时钟。
 *
 * 为什么需要它：scope（60s）和各页面列表（10s / 30s）是各自独立的轮询，
 * 顶栏如果只读 scope 的时间，就会显示一个"看起来刚刷新过、实际列表还没动"的时间，
 * 医生据此判断数据新鲜度是会被误导的。这里让每个数据源上报自己的 updatedAt，
 * 顶栏取其中最新的一次 —— 也就是"屏幕上的数据最晚是何时来的"。
 */

type Reporter = (key: string, at: number | null) => void;

const ReportContext = createContext<Reporter>(() => {});
const LatestContext = createContext<number | null>(null);

export function RefreshProvider({ children }: { children: React.ReactNode }) {
  const [clock, setClock] = useState<Record<string, number | null>>({});

  const report = useCallback<Reporter>((key, at) => {
    // 值没变就不触发重渲染：10s 轮询里大量 tick 的时间戳其实在变化，
    // 但同一次响应只上报一次，重复上报同一个值没有意义。
    setClock((prev) => (prev[key] === at ? prev : { ...prev, [key]: at }));
  }, []);

  const latest = useMemo(() => {
    const values = Object.values(clock).filter((v): v is number => typeof v === 'number');
    return values.length ? Math.max(...values) : null;
  }, [clock]);

  return (
    <ReportContext.Provider value={report}>
      <LatestContext.Provider value={latest}>{children}</LatestContext.Provider>
    </ReportContext.Provider>
  );
}

/** 顶栏读取：全部数据源里最新的一次刷新时间。 */
export function useRefreshClock(): number | null {
  return useContext(LatestContext);
}

/** 页面 / 范围上报自己的刷新时间（updatedAt 为 null 表示还没成功加载过）。 */
export function useReportRefresh(key: string, at: number | null): void {
  const report = useContext(ReportContext);
  useEffect(() => {
    report(key, at);
  }, [key, at, report]);
}
