import { useCallback, useEffect, useRef, useState } from 'react';
import { Virtuoso } from 'react-virtuoso';
import { ChevronDown, Clock, Coins, Route } from 'lucide-react';
import clsx from 'clsx';
import { statsApi, WORKER_LABELS, type AgentTrace } from '@his/shared';

const PAGE_SIZE = 30;

export function TracesPage() {
  const [traces, setTraces] = useState<AgentTrace[]>([]);
  const [hasMore, setHasMore] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const offsetRef = useRef(0);
  const loadingRef = useRef(false);

  const loadMore = useCallback(async () => {
    if (loadingRef.current || !hasMore) return;
    loadingRef.current = true;
    try {
      const r = await statsApi.traces({ limit: PAGE_SIZE, offset: offsetRef.current });
      setTraces((prev) => {
        const merged = offsetRef.current === 0 ? r.traces : [...prev, ...r.traces];
        return merged;
      });
      offsetRef.current += r.traces.length;
      setHasMore(r.traces.length === PAGE_SIZE);
    } catch {
      setHasMore(false);
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, [hasMore]);

  useEffect(() => {
    loadMore();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refresh = () => {
    offsetRef.current = 0;
    setTraces([]);
    setHasMore(true);
    setLoading(true);
    loadingRef.current = false;
    loadMore();
  };

  return (
    <div className="flex h-[calc(100dvh-6.5rem)] flex-col space-y-4 md:h-[calc(100dvh-3.5rem)] 2xl:h-[calc(100dvh-4rem)]">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-medium">Agent 轨迹</h1>
        <button
          onClick={refresh}
          className="rounded-xl border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
        >
          刷新
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-hidden rounded-2xl border border-gray-100 bg-white">
        {loading && traces.length === 0 ? (
          <div className="p-10 text-center text-sm text-gray-400">加载中…</div>
        ) : traces.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-12 text-center">
            <Route className="h-10 w-10 text-gray-300" />
            <div className="text-sm text-gray-400">
              暂无轨迹数据——跑一轮对话后，token 用量埋点会自动记录调用链
            </div>
          </div>
        ) : (
          <Virtuoso
            data={traces}
            className="h-full"
            endReached={loadMore}
            itemContent={(_, trace) => {
              const expanded = expandedId === trace.turn_id;
              return (
                <div className="border-b border-gray-50">
                  <button
                    onClick={() => setExpandedId(expanded ? null : trace.turn_id)}
                    className="flex w-full items-center gap-3 px-5 py-3 text-left hover:bg-gray-50/60"
                  >
                    <ChevronDown
                      className={clsx('h-4 w-4 shrink-0 text-gray-300 transition-transform', expanded && 'rotate-180')}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="truncate font-mono text-xs text-gray-400">
                          {trace.turn_id.slice(0, 12)}
                        </span>
                        <span className="rounded-full bg-primary-50 px-2 py-0.5 text-[10px] text-primary-600">
                          {trace.items.length} 次调用
                        </span>
                      </div>
                      <div className="mt-0.5 flex items-center gap-3 text-xs text-gray-400">
                        <span className="flex items-center gap-1">
                          <Clock className="h-3 w-3" />
                          {trace.created_at || '-'}
                        </span>
                        <span>会话 {trace.session_id?.slice(0, 12) || '-'}</span>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5 text-sm text-gray-600">
                      <Coins className="h-3.5 w-3.5 text-amber-400" />
                      {trace.total_tokens.toLocaleString()}
                    </div>
                  </button>

                  {/* 展开时间线 */}
                  {expanded && (
                    <div className="space-y-1.5 bg-gray-50/60 px-5 py-3">
                      {trace.items.map((item, i) => (
                        <div
                          key={i}
                          className="flex items-center gap-3 rounded-xl bg-white px-3 py-2 text-xs shadow-sm"
                        >
                          <span className="w-14 shrink-0 rounded-full bg-violet-50 px-2 py-0.5 text-center text-violet-600">
                            {WORKER_LABELS[item.worker] || item.worker}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-gray-500">{item.model || '-'}</span>
                          <span className="text-gray-400">
                            {item.prompt_tokens} + {item.completion_tokens} = {item.total_tokens} tokens
                          </span>
                          <span className="w-16 text-right text-gray-400">{item.latency_ms}ms</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            }}
          />
        )}
      </div>
    </div>
  );
}
