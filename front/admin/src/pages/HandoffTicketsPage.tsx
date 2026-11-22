import { useCallback, useEffect, useState } from 'react';
import clsx from 'clsx';
import { Headset, RefreshCw } from 'lucide-react';

import { adminHandoffApi, type HandoffQueue, type HandoffSessionMessage, type HandoffTicket } from '@his/shared';

const STATUS_META: Record<string, { label: string; cls: string }> = {
  open: { label: '待认领', cls: 'bg-amber-50 text-amber-700' },
  claimed: { label: '处理中', cls: 'bg-blue-50 text-blue-700' },
  resolved: { label: '已结单', cls: 'bg-emerald-50 text-emerald-700' },
  closed: { label: '已关闭', cls: 'bg-gray-100 text-gray-600' },
  cancelled: { label: '已撤回', cls: 'bg-gray-100 text-gray-500' },
};

const PRIORITY_META: Record<string, { label: string; cls: string }> = {
  urgent: { label: '紧急', cls: 'bg-red-50 text-red-600' },
  high: { label: '优先', cls: 'bg-orange-50 text-orange-600' },
  normal: { label: '常规', cls: 'bg-gray-100 text-gray-500' },
};

const REASON_LABEL: Record<string, string> = {
  patient_request: '患者主动要求',
  model_escalate: '模型判定转人工',
  emergency: '急症触发',
  unclear: '意图不明',
};

const FILTERS: { key: string; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'open', label: '待认领' },
  { key: 'claimed', label: '处理中' },
  { key: 'resolved', label: '已结单' },
  { key: 'cancelled', label: '已撤回' },
];

/** 会话消息在管理端只读展示：坐席说了什么、患者说了什么都要留痕 */
function MessageRow({ m }: { m: HandoffSessionMessage }) {
  const human = m.event_type === 'human_agent';
  const close = m.event_type === 'handoff_close';
  return (
    <div className="flex gap-2 py-1.5">
      <div className="w-16 shrink-0 pt-0.5 text-[11px] text-gray-400">
        {human ? '坐席' : close ? '系统' : m.role === 'user' ? '患者' : 'AI'}
      </div>
      <div
        className={clsx(
          'min-w-0 flex-1 rounded-lg px-2.5 py-1.5 text-[12px] leading-relaxed',
          human && 'bg-emerald-50 text-emerald-900',
          close && 'border border-dashed border-gray-200 bg-gray-50 text-gray-500',
          !human && !close && 'bg-gray-50 text-gray-700',
        )}
      >
        {m.content}
      </div>
    </div>
  );
}

export function HandoffTicketsPage() {
  const [data, setData] = useState<HandoffQueue | null>(null);
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<HandoffTicket | null>(null);
  const [messages, setMessages] = useState<HandoffSessionMessage[]>([]);
  const [msgLoading, setMsgLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await adminHandoffApi.tickets({ status, only_active: false, limit: 100 });
      setData(r);
      setError('');
      // 选中的工单变了状态要跟着更新，否则右侧详情停在旧状态
      setSelected((cur) => (cur ? r.items.find((t) => t.id === cur.id) ?? cur : cur));
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!selected) {
      setMessages([]);
      return;
    }
    let cancelled = false;
    setMsgLoading(true);
    adminHandoffApi
      .messages(selected.id, 40)
      .then((r) => {
        if (!cancelled) setMessages(r.items);
      })
      .catch(() => {
        if (!cancelled) setMessages([]);
      })
      .finally(() => {
        if (!cancelled) setMsgLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected?.id]);

  const stats = data?.stats;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">人工接管</h1>
          <p className="mt-0.5 text-xs text-gray-400">
            患者「转人工」后进入这张工单排队，坐席认领后回复会直接写进患者会话 · 管理端只读审计，不参与认领
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {stats && (
            <>
              <span className="rounded-lg bg-amber-50 px-2.5 py-1 text-xs font-medium text-amber-700">
                待认领 {stats.open}
              </span>
              <span className="rounded-lg bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-700">
                处理中 {stats.claimed}
              </span>
              <span className="rounded-lg bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">
                已结单 {stats.resolved}
              </span>
              <span className="rounded-lg bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-600">
                已撤回 {stats.cancelled}
              </span>
            </>
          )}
          <button
            onClick={() => void load()}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-xl border border-gray-200 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-60"
          >
            <RefreshCw className={clsx('h-3.5 w-3.5', loading && 'animate-spin')} />
            刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-100 bg-red-50 px-3 py-2 text-xs text-red-600">{error}</div>
      )}

      <div className="flex flex-wrap items-center gap-1 rounded-xl border border-gray-100 bg-white p-1">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setStatus(f.key)}
            className={clsx(
              'rounded-lg px-3 py-1.5 text-xs transition-colors',
              status === f.key ? 'bg-primary-500 font-medium text-white' : 'text-gray-600 hover:bg-gray-50',
            )}
          >
            {f.label}
          </button>
        ))}
        <span className="ml-auto pr-2 text-[11px] text-gray-400">匹配 {data?.total ?? 0} 张</span>
      </div>

      <div className="flex flex-col gap-4 xl:flex-row">
        {/* 列表 */}
        <div className="min-w-0 flex-1 overflow-hidden rounded-2xl border border-gray-100 bg-white">
          <table className="w-full text-xs">
            <thead className="bg-gray-50 text-[11px] text-gray-500">
              <tr>
                <th className="px-3 py-2 text-left font-medium">工单号</th>
                <th className="px-3 py-2 text-left font-medium">患者</th>
                <th className="px-3 py-2 text-left font-medium">状态</th>
                <th className="px-3 py-2 text-left font-medium">来源</th>
                <th className="px-3 py-2 text-left font-medium">认领人</th>
                <th className="px-3 py-2 text-right font-medium">回复</th>
                <th className="px-3 py-2 text-left font-medium">创建</th>
                <th className="px-3 py-2 text-left font-medium">办结</th>
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((t) => {
                const st = STATUS_META[t.status] ?? { label: t.status, cls: 'bg-gray-100 text-gray-500' };
                const pr = PRIORITY_META[t.priority] ?? PRIORITY_META.normal;
                return (
                  <tr
                    key={t.id}
                    onClick={() => setSelected(t)}
                    className={clsx(
                      'cursor-pointer border-t border-gray-50 hover:bg-gray-50',
                      selected?.id === t.id && 'bg-primary-50/40',
                    )}
                  >
                    <td className="px-3 py-2 font-mono text-[11px] text-gray-700">{t.ticket_no}</td>
                    <td className="px-3 py-2 text-gray-600">#{t.patient_id}</td>
                    <td className="px-3 py-2">
                      <span className={clsx('rounded px-1.5 py-0.5 text-[11px]', st.cls)}>{st.label}</span>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-1">
                        <span className="text-gray-600">{REASON_LABEL[t.reason] || t.reason}</span>
                        {t.priority !== 'normal' && (
                          <span className={clsx('rounded px-1 py-0.5 text-[10px]', pr.cls)}>{pr.label}</span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2 text-gray-600">{t.assigned_name || '—'}</td>
                    <td className="px-3 py-2 text-right tabular-nums text-gray-600">{t.message_count}</td>
                    <td className="px-3 py-2 text-gray-400">{t.created_at.slice(5, 16)}</td>
                    <td className="px-3 py-2 text-gray-400">{t.resolved_at ? t.resolved_at.slice(5, 16) : '—'}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!loading && (data?.items ?? []).length === 0 && (
            <div className="flex flex-col items-center gap-1 py-12 text-xs text-gray-400">
              <Headset className="h-6 w-6" />
              当前筛选下没有工单
            </div>
          )}
        </div>

        {/* 只读详情 */}
        <div className="w-full shrink-0 rounded-2xl border border-gray-100 bg-white p-4 xl:w-[380px]">
          {!selected ? (
            <div className="py-10 text-center text-xs text-gray-400">点左侧任意一行查看工单详情</div>
          ) : (
            <div className="space-y-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="font-mono text-xs text-gray-700">{selected.ticket_no}</span>
                  <span
                    className={clsx(
                      'rounded px-1.5 py-0.5 text-[11px]',
                      (STATUS_META[selected.status] ?? STATUS_META.closed).cls,
                    )}
                  >
                    {(STATUS_META[selected.status] ?? { label: selected.status }).label}
                  </span>
                </div>
                <div className="mt-1 text-[11px] text-gray-400">
                  患者 #{selected.patient_id} · 会话 {selected.session_id} · {REASON_LABEL[selected.reason] || selected.reason}
                </div>
              </div>

              <div className="rounded-lg bg-gray-50 px-3 py-2 text-[12px] leading-relaxed text-gray-700">
                {selected.summary || '（无摘要）'}
              </div>

              <div className="grid grid-cols-2 gap-y-1.5 text-[11px]">
                <span className="text-gray-400">建单</span>
                <span className="text-gray-700">{selected.created_at || '—'}</span>
                <span className="text-gray-400">认领</span>
                <span className="text-gray-700">
                  {selected.assigned_name || '—'} {selected.claimed_at ? `· ${selected.claimed_at}` : ''}
                </span>
                <span className="text-gray-400">办结</span>
                <span className="text-gray-700">{selected.resolved_at || '—'}</span>
                <span className="text-gray-400">处理说明</span>
                <span className="text-gray-700">{selected.resolution || '—'}</span>
              </div>

              <div>
                <div className="mb-1 text-[11px] font-medium text-gray-500">
                  会话记录 {msgLoading ? '· 加载中…' : `· ${messages.length} 条`}
                </div>
                <div className="max-h-[360px] overflow-y-auto">
                  {messages.length === 0 && !msgLoading ? (
                    <div className="py-4 text-center text-[11px] text-gray-400">这张单还没有落库会话消息</div>
                  ) : (
                    messages.map((m) => <MessageRow key={m.seq} m={m} />)
                  )}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
