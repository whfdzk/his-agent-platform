import { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  Gauge,
  RefreshCw,
  ShieldAlert,
  Trash2,
  XCircle,
} from 'lucide-react';
import { observabilityApi, safetyApi } from '@his/shared';
import type {
  SafetyEventItem,
  SafetyRuleItem,
  SafetyStats,
  SystemLimits,
} from '@his/shared';

const LEVEL_STYLE: Record<string, string> = {
  critical: 'bg-red-50 text-red-600',
  high: 'bg-orange-50 text-orange-600',
  warn: 'bg-amber-50 text-amber-600',
  info: 'bg-slate-100 text-slate-500',
};

const ACTION_LABEL: Record<string, string> = {
  block: '已拦截',
  escalate: '转人工',
  warn: '已提示',
  allow: '放行',
};

export function SafetyPage() {
  const [tab, setTab] = useState<'pending' | 'all'>('pending');
  const [events, setEvents] = useState<SafetyEventItem[]>([]);
  const [stats, setStats] = useState<SafetyStats | null>(null);
  const [rules, setRules] = useState<SafetyRuleItem[]>([]);
  const [limits, setLimits] = useState<SystemLimits | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState('');
  const [showRules, setShowRules] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [eventRes, statsRes] = await Promise.all([
        tab === 'pending' ? safetyApi.pending(100) : safetyApi.events({ days: 30, limit: 100 }),
        safetyApi.stats(30),
      ]);
      setEvents(eventRes.events);
      setStats(statsRes);
    } catch (e) {
      setMessage((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // 规则库与限额在页面首次进入时加载一次
    void Promise.all([safetyApi.rules(), safetyApi.limits()])
      .then(([r, l]) => {
        setRules(r.rules);
        setLimits(l);
      })
      .catch(() => undefined);
  }, []);

  const review = async (event: SafetyEventItem, decision: 'approved' | 'rejected') => {
    try {
      await safetyApi.review(event.id, decision, note || undefined);
      setMessage(decision === 'approved' ? '已确认拦截合理' : '已标记为误报');
      setExpanded(null);
      setNote('');
      await load();
    } catch (e) {
      setMessage((e as Error).message || '操作失败');
    }
  };

  const purge = async () => {
    if (!window.confirm('将按保留周期清理过期安全事件（链路轨迹由 Langfuse 自行管理），确定执行？')) return;
    try {
      const res = await observabilityApi.purge();
      setMessage(`已清理安全事件 ${res.deleted_safety_events} 条`);
    } catch (e) {
      setMessage((e as Error).message || '清理失败');
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">安全审核</h1>
          <p className="mt-0.5 text-xs text-gray-400">
            危险信号命中、输出审核与人工复核；所有片段均已脱敏
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={purge}
            className="flex items-center gap-1 rounded-xl border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
          >
            <Trash2 className="h-3.5 w-3.5" /> 执行保留期清理
          </button>
          <button
            onClick={load}
            className="flex items-center gap-1 rounded-xl border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
          >
            <RefreshCw className="h-3.5 w-3.5" /> 刷新
          </button>
        </div>
      </div>

      {message && (
        <div className="rounded-xl bg-primary-50 px-3 py-2 text-xs text-primary-700">{message}</div>
      )}

      {/* 概览 */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="近 30 天事件" value={stats?.total ?? 0} />
        <StatCard label="待人工复核" value={stats?.pending_review ?? 0} tone="amber" />
        <StatCard
          label="拦截次数"
          value={stats?.by_action.find((a) => a.action === 'block')?.count ?? 0}
          tone="red"
        />
        <StatCard
          label="高危 / 危急"
          value={
            (stats?.by_level.find((l) => l.level === 'critical')?.count ?? 0) +
            (stats?.by_level.find((l) => l.level === 'high')?.count ?? 0)
          }
          tone="orange"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        {/* 事件列表 */}
        <div className="lg:col-span-2">
          <div className="mb-2 flex gap-1 rounded-xl bg-gray-100 p-1 text-xs">
            {(
              [
                ['pending', '待复核'],
                ['all', '全部事件'],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setTab(key)}
                className={clsx(
                  'flex-1 rounded-lg py-1.5 transition-colors',
                  tab === key ? 'bg-white font-medium text-gray-900 shadow-sm' : 'text-gray-500',
                )}
              >
                {label}
              </button>
            ))}
          </div>

          <div className="space-y-2">
            {loading ? (
              <div className="rounded-2xl bg-white p-6 text-center text-xs text-gray-400">加载中…</div>
            ) : events.length === 0 ? (
              <div className="flex flex-col items-center gap-2 rounded-2xl bg-white p-10 text-center">
                <CheckCircle2 className="h-8 w-8 text-emerald-400" />
                <div className="text-sm text-gray-600">
                  {tab === 'pending' ? '没有待复核事件' : '暂无安全事件'}
                </div>
                <p className="text-xs text-gray-400">危险信号与输出违规会自动进入这里</p>
              </div>
            ) : (
              events.map((e) => (
                <div key={e.id} className="rounded-2xl bg-white p-4">
                  <button
                    onClick={() => {
                      setExpanded(expanded === e.id ? null : e.id);
                      setNote('');
                    }}
                    className="flex w-full items-start gap-3 text-left"
                  >
                    <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gray-50 text-gray-400">
                      {e.action === 'block' ? (
                        <ShieldAlert className="h-4 w-4 text-red-500" />
                      ) : (
                        <AlertTriangle className="h-4 w-4 text-amber-500" />
                      )}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <span className={clsx('rounded-full px-2 py-0.5 text-[11px]', LEVEL_STYLE[e.level])}>
                          {e.level}
                        </span>
                        <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">
                          {e.stage === 'input' ? '输入审核' : e.stage === 'output' ? '输出审核' : '工具审核'}
                        </span>
                        <span className="text-[11px] text-gray-500">{ACTION_LABEL[e.action] || e.action}</span>
                        <span className="text-xs font-medium text-gray-800">{e.rule_name}</span>
                      </div>
                      {e.snippet && (
                        <div className="mt-1 truncate text-[11px] text-gray-400">片段：{e.snippet}</div>
                      )}
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-gray-400">
                        <span className="flex items-center gap-0.5">
                          <Clock className="h-3 w-3" />
                          {e.created_at?.replace('T', ' ').slice(0, 19)}
                        </span>
                        <span>用户 {e.user_id || '—'}</span>
                        <span>trace {e.trace_id ? `${e.trace_id.slice(0, 12)}…` : '—'}</span>
                        {e.review_status !== 'none' && (
                          <span
                            className={clsx(
                              'rounded-full px-1.5 py-0.5',
                              e.review_status === 'approved'
                                ? 'bg-emerald-50 text-emerald-600'
                                : e.review_status === 'pending'
                                  ? 'bg-amber-50 text-amber-600'
                                  : 'bg-gray-100 text-gray-500',
                            )}
                          >
                            {e.review_status === 'approved'
                              ? '已确认'
                              : e.review_status === 'pending'
                                ? '待复核'
                                : '误报'}
                          </span>
                        )}
                      </div>
                    </div>
                  </button>

                  {expanded === e.id && (
                    <div className="mt-3 space-y-2 border-t border-gray-100 pt-3">
                      <div className="text-xs text-gray-500">
                        规则 ID：<span className="font-mono text-gray-700">{e.rule_id}</span> · 分类 {e.category}
                      </div>
                      {e.review_note && (
                        <div className="rounded-lg bg-gray-50 px-2 py-1.5 text-[11px] text-gray-500">
                          复核意见：{e.review_note}
                        </div>
                      )}
                      {e.review_status === 'pending' && (
                        <>
                          <input
                            value={note}
                            onChange={(ev) => setNote(ev.target.value)}
                            placeholder="复核意见（可选）"
                            className="w-full rounded-lg border border-gray-200 px-2.5 py-1.5 text-xs focus:border-primary-500 focus:outline-none"
                          />
                          <div className="flex gap-2">
                            <button
                              onClick={() => review(e, 'approved')}
                              className="flex items-center gap-1 rounded-lg bg-emerald-50 px-3 py-1.5 text-xs text-emerald-600 hover:bg-emerald-100"
                            >
                              <CheckCircle2 className="h-3.5 w-3.5" /> 拦截合理
                            </button>
                            <button
                              onClick={() => review(e, 'rejected')}
                              className="flex items-center gap-1 rounded-lg bg-gray-100 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-200"
                            >
                              <XCircle className="h-3.5 w-3.5" /> 标记误报
                            </button>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </div>

        {/* 右侧：top 规则 + 限额 */}
        <div className="space-y-3">
          <div className="rounded-2xl bg-white p-4">
            <div className="mb-2 text-xs font-medium text-gray-700">命中 Top 规则</div>
            <div className="space-y-1.5">
              {(stats?.top_rules || []).length === 0 ? (
                <div className="text-[11px] text-gray-400">暂无命中</div>
              ) : (
                stats?.top_rules.map((r) => (
                  <div key={r.rule_id} className="flex items-center justify-between text-[11px]">
                    <span className="truncate text-gray-600">{r.rule_name || r.rule_id}</span>
                    <span className="ml-2 shrink-0 text-gray-400">{r.count}</span>
                  </div>
                ))
              )}
            </div>
          </div>

          <div className="rounded-2xl bg-white p-4">
            <div className="mb-2 flex items-center gap-1 text-xs font-medium text-gray-700">
              <Gauge className="h-3.5 w-3.5" /> 执行限额
            </div>
            {limits ? (
              <div className="space-y-1.5 text-[11px] text-gray-500">
                <Row label="Agent 调用" value={`≤ ${limits.agent_max_calls} 次/轮`} />
                <Row label="工具调用" value={`≤ ${limits.tool_max_calls} 次/轮`} />
                <Row label="Token" value={`≤ ${limits.turn_max_tokens}/轮`} />
                <Row label="执行时长" value={`≤ ${limits.turn_timeout_seconds}s`} />
                <Row label="检索结果" value={`≤ ${limits.retrieval_max_results} 条`} />
                <Row label="并发任务" value={`≤ ${limits.user_max_concurrent_tasks}/用户`} />
                <Row label="轨迹保留" value={`${limits.trace_retention_days} 天`} />
                <Row label="安全事件保留" value={`${limits.safety_event_retention_days} 天`} />
                <Row label="访问日志保留" value={`${limits.access_log_retention_days} 天`} />
              </div>
            ) : (
              <div className="text-[11px] text-gray-400">加载中…</div>
            )}
          </div>

          <div className="rounded-2xl bg-white p-4">
            <button
              onClick={() => setShowRules((v) => !v)}
              className="flex w-full items-center justify-between text-xs font-medium text-gray-700"
            >
              危险信号规则库
              <span className="text-[11px] font-normal text-gray-400">{rules.length} 条</span>
            </button>
            {showRules && (
              <div className="mt-2 max-h-72 space-y-1 overflow-y-auto">
                {rules.map((r) => (
                  <div key={r.id} className="flex items-center gap-1.5 text-[11px]">
                    <span className={clsx('rounded px-1 py-0.5', LEVEL_STYLE[r.level])}>{r.level}</span>
                    <span className="truncate text-gray-600">{r.name}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function StatCard({ label, value, tone }: { label: string; value: number; tone?: string }) {
  const tones: Record<string, string> = {
    amber: 'text-amber-600',
    red: 'text-red-600',
    orange: 'text-orange-600',
  };
  return (
    <div className="rounded-2xl bg-white p-3.5">
      <div className="text-[11px] text-gray-400">{label}</div>
      <div className={clsx('mt-1 text-xl font-semibold', tone ? tones[tone] : 'text-gray-800')}>
        {value}
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <span>{label}</span>
      <span className="text-gray-700">{value}</span>
    </div>
  );
}
