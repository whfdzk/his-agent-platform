import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Virtuoso } from 'react-virtuoso';
import clsx from 'clsx';
import { statsApi, WORKER_LABELS, type TokenUsageGroup } from '@his/shared';

const PIE_COLORS = ['#2b7fff', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'];

const RANGES = [
  { label: '近 7 天', days: 7 },
  { label: '近 30 天', days: 30 },
  { label: '近 90 天', days: 90 },
];

function dateStr(daysAgo: number) {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  return d.toISOString().slice(0, 10);
}

export function TokenUsagePage() {
  const [days, setDays] = useState(7);
  const [byDay, setByDay] = useState<TokenUsageGroup[]>([]);
  const [byModel, setByModel] = useState<TokenUsageGroup[]>([]);
  const [byWorker, setByWorker] = useState<TokenUsageGroup[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const params = { start: dateStr(days), end: dateStr(0) };
    const [d, m, w] = await Promise.all([
      statsApi.tokens({ ...params, group_by: 'day' }).catch(() => ({ groups: [] as TokenUsageGroup[] })),
      statsApi.tokens({ ...params, group_by: 'model' }).catch(() => ({ groups: [] as TokenUsageGroup[] })),
      statsApi.tokens({ ...params, group_by: 'worker' }).catch(() => ({ groups: [] as TokenUsageGroup[] })),
    ]);
    setByDay(d.groups);
    setByModel(m.groups);
    setByWorker(w.groups);
    setLoading(false);
  }, [days]);

  useEffect(() => {
    load();
  }, [load]);

  const totalTokens = byWorker.reduce((s, g) => s + g.total_tokens, 0);
  const totalCalls = byWorker.reduce((s, g) => s + g.calls, 0);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-medium">Token 用量统计</h1>
        <div className="flex rounded-xl border border-gray-200 bg-white p-0.5">
          {RANGES.map((r) => (
            <button
              key={r.days}
              onClick={() => setDays(r.days)}
              className={clsx(
                'rounded-lg px-3 py-1.5 text-xs transition-colors',
                days === r.days ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50',
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* 汇总 */}
      <div className="grid grid-cols-3 gap-4">
        <div className="rounded-2xl border border-gray-100 bg-white p-5">
          <div className="text-xs text-gray-400">总 Token</div>
          <div className="text-xl font-medium">{totalTokens.toLocaleString()}</div>
        </div>
        <div className="rounded-2xl border border-gray-100 bg-white p-5">
          <div className="text-xs text-gray-400">LLM 调用次数</div>
          <div className="text-xl font-medium">{totalCalls.toLocaleString()}</div>
        </div>
        <div className="rounded-2xl border border-gray-100 bg-white p-5">
          <div className="text-xs text-gray-400">模型数</div>
          <div className="text-xl font-medium">{byModel.length}</div>
        </div>
      </div>

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">
          加载中…
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 按天趋势 */}
            <div className="rounded-2xl border border-gray-100 bg-white p-5">
              <h2 className="mb-4 text-sm font-medium">每日消耗（prompt / completion）</h2>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={byDay}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="key" tick={{ fontSize: 11 }} tickFormatter={(v: string) => v.slice(5)} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="prompt_tokens" name="输入" stackId="a" fill="#2b7fff" radius={[0, 0, 4, 4]} />
                  <Bar dataKey="completion_tokens" name="输出" stackId="a" fill="#22c55e" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* 按模型 */}
            <div className="rounded-2xl border border-gray-100 bg-white p-5">
              <h2 className="mb-4 text-sm font-medium">按模型汇总</h2>
              <ResponsiveContainer width="100%" height={260}>
                <PieChart>
                  <Pie data={byModel} dataKey="total_tokens" nameKey="key" innerRadius={50} outerRadius={90} paddingAngle={2}>
                    {byModel.map((_, i) => (
                      <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* 按 worker */}
            <div className="rounded-2xl border border-gray-100 bg-white p-5">
              <h2 className="mb-4 text-sm font-medium">按 Agent 来源汇总</h2>
              <ResponsiveContainer width="100%" height={260}>
                <BarChart data={byWorker}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis
                    dataKey="key"
                    tick={{ fontSize: 11 }}
                    tickFormatter={(v: string) => WORKER_LABELS[v] || v}
                  />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Bar dataKey="prompt_tokens" name="输入" stackId="a" fill="#8b5cf6" radius={[0, 0, 4, 4]} />
                  <Bar dataKey="completion_tokens" name="输出" stackId="a" fill="#06b6d4" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* 明细表（按天） */}
            <div className="flex flex-col rounded-2xl border border-gray-100 bg-white p-5">
              <h2 className="mb-3 text-sm font-medium">每日明细</h2>
              <div className="min-h-0 flex-1">
                <Virtuoso
                  data={byDay}
                  className="h-full"
                  totalCount={byDay.length}
                  itemContent={(_, g) => (
                    <div className="flex items-center justify-between border-b border-gray-50 px-1 py-2.5 text-sm">
                      <span className="font-mono text-xs text-gray-500">{g.key}</span>
                      <span className="text-gray-600">
                        {g.total_tokens.toLocaleString()} tokens
                      </span>
                      <span className="text-xs text-gray-400">{g.calls} 次调用</span>
                      <span className="text-xs text-gray-400">均 {g.avg_latency_ms}ms</span>
                    </div>
                  )}
                />
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
