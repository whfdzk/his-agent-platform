import { useEffect, useMemo, useRef, useState } from 'react';
import dayjs from 'dayjs';
import {
  ArrowRight,
  CheckCircle2,
  Clock,
  Inbox,
  MessageSquare,
  RefreshCw,
  Send,
  Sparkles,
  UserCheck,
} from 'lucide-react';
import clsx from 'clsx';

import {
  getStoredToken,
  handoffApi,
  streamHandoff,
  type HandoffQueue,
  type HandoffSessionMessage,
  type HandoffTicket,
  type HandoffTicketStatus,
} from '@his/shared';

import { usePolling } from '../hooks/usePolling';
import { Button, Card, EmptyState, InlineError, Tag } from '../components/ui';

const STATUS_META: Record<HandoffTicketStatus, { label: string; tone: 'amber' | 'blue' | 'emerald' | 'slate' }> = {
  open: { label: '待认领', tone: 'amber' },
  claimed: { label: '处理中', tone: 'blue' },
  resolved: { label: '已结单', tone: 'emerald' },
  closed: { label: '已关闭', tone: 'slate' },
  cancelled: { label: '已撤回', tone: 'slate' },
};

const PRIORITY_META: Record<string, { label: string; tone: 'red' | 'amber' | 'slate' }> = {
  urgent: { label: '紧急', tone: 'red' },
  high: { label: '优先', tone: 'amber' },
  normal: { label: '常规', tone: 'slate' },
};

const REASON_LABEL: Record<string, string> = {
  patient_request: '患者主动要求',
  model_decision: '模型判断',
  risk_alert: '风险兜底',
};

type ViewKey = 'open' | 'mine' | 'all';

/** 链路节点：每一格都是"这一步有没有发生 + 证据是什么"，不是装饰 */
interface ChainNode {
  key: string;
  title: string;
  detail: string;
  at?: string;
  state: 'done' | 'current' | 'waiting' | 'skipped';
}

function statusRank(status: HandoffTicketStatus): number {
  return { open: 1, claimed: 2, resolved: 4, closed: 4, cancelled: 2 }[status] ?? 0;
}

/**
 * 把工单字段翻译成一条可读的链路。
 *
 * 关键判断：`payload.source === 'supervisor_tool'` 才是"模型自己决定转人工、图被 interrupt 挂住"；
 * 否则是患者点按钮/接口建单（工单存在，但当轮对话没被挂起）。这两种链路的节点不一样，
 * 混着画会让人误以为"挂起"一定发生过。
 */
function buildChain(t: HandoffTicket, hasClose: boolean): ChainNode[] {
  const fromModel = String(t.payload?.source || '') === 'supervisor_tool';
  const rank = statusRank(t.status);
  const cancelled = t.status === 'cancelled';

  return [
    {
      key: 'ask',
      title: '患者提问',
      detail: `会话 ${t.session_id}`,
      at: t.created_at,
      state: 'done',
    },
    {
      key: 'decide',
      title: fromModel ? '模型判定转人工' : '患者申请转人工',
      detail: fromModel ? '调用 request_human_handoff' : REASON_LABEL[t.reason] || t.reason,
      at: t.created_at,
      state: 'done',
    },
    {
      key: 'ticket',
      title: '建单',
      detail: `${t.ticket_no} · ${PRIORITY_META[t.priority]?.label || t.priority}`,
      at: t.created_at,
      state: 'done',
    },
    {
      key: 'interrupt',
      title: '图挂起',
      detail: fromModel ? '停在 handoff_interrupt.after_model' : '未挂起（接口建单）',
      at: fromModel ? t.created_at : undefined,
      state: fromModel ? 'done' : 'skipped',
    },
    {
      key: 'claim',
      title: '坐席认领',
      detail: t.assigned_name ? `${t.assigned_name}（#${t.assigned_to}）` : '还没人接',
      at: t.claimed_at,
      state: cancelled ? 'skipped' : rank >= 2 ? 'done' : 'waiting',
    },
    {
      key: 'reply',
      title: '坐席回复',
      detail: t.message_count > 0 ? `${t.message_count} 条` : '尚未回复',
      at: t.last_message_at,
      state: t.message_count > 0 ? 'done' : rank >= 2 ? 'current' : 'waiting',
    },
    {
      key: 'resolve',
      title: cancelled ? '患者撤回' : '结单',
      detail: cancelled ? '挂起的一轮已放掉' : t.resolution || '待填写处理说明',
      at: t.resolved_at,
      state: cancelled ? 'done' : rank >= 4 ? 'done' : 'waiting',
    },
    {
      key: 'resume',
      title: '续跑 + 收尾',
      detail: hasClose ? 'closing 已写回会话' : cancelled ? '撤回话术待写回' : '等待结单',
      state: hasClose ? 'done' : cancelled || rank >= 4 ? 'current' : 'waiting',
    },
  ];
}

function ChainView({ nodes }: { nodes: ChainNode[] }) {
  return (
    <div className="flex flex-wrap items-stretch gap-2">
      {nodes.map((n, i) => (
        <div key={n.key} className="flex items-stretch gap-2">
          <div
            className={clsx(
              'min-w-[124px] rounded-lg border px-2.5 py-2',
              n.state === 'done' && 'border-emerald-100 bg-emerald-50/60',
              n.state === 'current' && 'border-primary-200 bg-primary-50',
              n.state === 'waiting' && 'border-slate-200 bg-white',
              n.state === 'skipped' && 'border-dashed border-slate-200 bg-slate-50/60',
            )}
          >
            <div className="flex items-center gap-1">
              <span
                className={clsx(
                  'flex h-4 w-4 items-center justify-center rounded-full text-[10px] font-semibold',
                  n.state === 'done' && 'bg-emerald-500 text-white',
                  n.state === 'current' && 'bg-primary-600 text-white',
                  n.state === 'waiting' && 'bg-slate-200 text-slate-500',
                  n.state === 'skipped' && 'bg-slate-100 text-slate-400',
                )}
              >
                {i + 1}
              </span>
              <span
                className={clsx(
                  'text-[12px] font-medium',
                  n.state === 'done' && 'text-emerald-700',
                  n.state === 'current' && 'text-primary-800',
                  n.state === 'waiting' && 'text-slate-500',
                  n.state === 'skipped' && 'text-slate-400',
                )}
              >
                {n.title}
              </span>
            </div>
            <div className="mt-1 line-clamp-2 max-w-[150px] text-[11px] leading-snug text-slate-500" title={n.detail}>
              {n.detail}
            </div>
            <div className="mt-0.5 text-[10px] text-slate-400">{n.at ? n.at.slice(5, 16) : '—'}</div>
          </div>
          {i < nodes.length - 1 && (
            <ArrowRight
              className={clsx('mt-4 h-3.5 w-3.5 shrink-0', n.state === 'done' ? 'text-emerald-400' : 'text-slate-300')}
            />
          )}
        </div>
      ))}
    </div>
  );
}

function MessageBubble({ m }: { m: HandoffSessionMessage }) {
  const human = m.event_type === 'human_agent';
  const closing = m.event_type === 'handoff_close';
  if (closing) {
    return (
      <div className="my-1 rounded-lg border border-violet-100 bg-violet-50/70 px-3 py-2 text-[12px] leading-relaxed text-violet-800">
        <span className="mr-1 inline-flex items-center gap-1 text-[11px] font-medium">
          <Sparkles className="h-3 w-3" /> 系统收尾
        </span>
        {m.content}
      </div>
    );
  }
  const isUser = m.role === 'user';
  return (
    <div className={clsx('flex', isUser ? 'justify-start' : 'justify-end')}>
      <div
        className={clsx(
          'max-w-[85%] rounded-lg px-3 py-2 text-[12px] leading-relaxed',
          isUser && 'bg-slate-100 text-slate-700',
          !isUser && !human && 'border border-slate-100 bg-white text-slate-600',
          human && 'bg-primary-600 text-white',
        )}
      >
        <div className={clsx('mb-0.5 text-[10px]', human ? 'text-primary-100' : 'text-slate-400')}>
          {human ? '坐席' : isUser ? '患者' : 'AI'} · #{m.seq}
        </div>
        {m.content}
      </div>
    </div>
  );
}

export function HandoffPage() {
  const [view, setView] = useState<ViewKey>('open');
  const [selected, setSelected] = useState<number | null>(null);
  const [reply, setReply] = useState('');
  const [resolution, setResolution] = useState('');
  const [busy, setBusy] = useState('');
  const [actionError, setActionError] = useState('');

  const params = useMemo(() => {
    if (view === 'open') return { status: 'open', only_active: true, limit: 50 };
    if (view === 'mine') return { mine: true, only_active: true, limit: 50 };
    return { only_active: false, limit: 50 };
  }, [view]);

  const { data, error, loading, updatedAt, refresh } = usePolling<HandoffQueue>(
    () => handoffApi.queue(params),
    10_000,
  );

  const items = data?.items ?? [];

  // 选中的工单可能在刷新后消失（被结单/撤回），这时退回第一条，别让右侧空着
  useEffect(() => {
    if (selected && items.some((t) => t.id === selected)) return;
    setSelected(items.length > 0 ? items[0].id : null);
  }, [items, selected]);

  const current = items.find((t) => t.id === selected) ?? null;

  const { data: msgData, refresh: refreshMsgs } = usePolling(
    () => (selected ? handoffApi.messages(selected, 40) : Promise.resolve({ items: [], total: 0 })),
    8_000,
  );
  const messages = msgData?.items ?? [];
  const hasClose = messages.some((m) => m.event_type === 'handoff_close');

  // 换选中工单要立刻拉一次消息：usePolling 的 8s 档期会让右侧先空 8 秒，
  // 看起来像"这张单没有对话"，其实只是还没轮到下一次轮询
  useEffect(() => {
    void refreshMsgs();
  }, [selected, refreshMsgs]);

  const chain = current ? buildChain(current, hasClose) : [];

  // 过滤条切换后立刻拉一次，避免"筛选条件已变、列表还是旧的"
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    void refresh();
  }, [view, refresh]);

  /**
   * C3 实时：订阅工单推送（scope=seat 只收"队列变了"信号），有人建单 /
   * 认领 / 回复 / 结单时立刻重拉队列与当前工单消息。
   * 10s 轮询保留作兜底：推送断线（浏览器休眠、网关超时）时页面不会停在旧数据。
   * 350ms 去抖：一次结单会连发"工单变更 + 收尾话术"两条事件，不必拉两遍。
   */
  useEffect(() => {
    let stopped = false;
    let ctrl: AbortController | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let debounce: ReturnType<typeof setTimeout> | null = null;

    const wake = () => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(() => {
        void refresh();
        void refreshMsgs();
      }, 350);
    };

    const connect = () => {
      if (stopped) return;
      ctrl = new AbortController();
      void streamHandoff({
        scope: 'seat',
        token: getStoredToken(),
        signal: ctrl.signal,
        onEvent: (ev) => {
          if (ev.type === 'queue') wake();
        },
        onClose: () => {
          if (!stopped) retry = setTimeout(connect, 5_000);
        },
      });
    };

    connect();
    return () => {
      stopped = true;
      ctrl?.abort();
      if (retry) clearTimeout(retry);
      if (debounce) clearTimeout(debounce);
    };
  }, [refresh, refreshMsgs]);

  async function afterAction(fn: () => Promise<unknown>, tag: string) {
    setBusy(tag);
    setActionError('');
    try {
      await fn();
      await Promise.all([refresh(), refreshMsgs()]);
    } catch (e) {
      setActionError((e as Error).message || '操作失败');
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-base font-semibold text-slate-900">人工坐席</h1>
          <p className="mt-0.5 text-xs text-slate-400">
            患者「转人工」后模型会挂起当前回合，工单在这里排队；你回复的每一句都会直接出现在患者会话里。
          </p>
        </div>
        <div className="flex items-center gap-2">
          {data && (
            <div className="flex items-center gap-1.5 text-[11px] text-slate-500">
              <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700">待认领 {data.stats.open}</span>
              <span className="rounded bg-primary-50 px-1.5 py-0.5 font-medium text-primary-700">
                处理中 {data.stats.claimed}
              </span>
              <span className="rounded bg-emerald-50 px-1.5 py-0.5 font-medium text-emerald-700">
                已结单 {data.stats.resolved}
              </span>
            </div>
          )}
          <Button variant="secondary" size="sm" onClick={() => void refresh()} disabled={loading}>
            <RefreshCw className={clsx('h-3.5 w-3.5', loading && 'animate-spin')} />
            刷新
          </Button>
        </div>
      </div>

      {(error || actionError) && <InlineError text={actionError || error} />}

      <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1">
        {(
          [
            ['open', '待认领', data?.stats.open],
            ['mine', '我处理中', data?.stats.claimed],
            ['all', '全部（含历史）', undefined],
          ] as [ViewKey, string, number | undefined][]
        ).map(([k, label, count]) => (
          <button
            key={k}
            type="button"
            onClick={() => setView(k)}
            className={clsx(
              'rounded px-2.5 py-1 text-[11px] transition-colors',
              view === k ? 'bg-primary-50 font-medium text-primary-700' : 'text-slate-500 hover:bg-slate-50',
            )}
          >
            {label}
            {count !== undefined && count > 0 ? ` ${count}` : ''}
          </button>
        ))}
        <span className="ml-auto pr-1 text-[10px] text-slate-400">
          最近刷新 {updatedAt ? dayjs(updatedAt).format('HH:mm:ss') : '—'}
        </span>
      </div>

      {items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Inbox className="h-5 w-5" />}
            title={view === 'mine' ? '你名下没有在办工单' : '当前没有待处理的工单'}
            description="患者说「转人工/找真人」时会自动建单；同一会话在办期间只会有一张单。"
          />
        </Card>
      ) : (
        <div className="grid gap-3 lg:grid-cols-[300px,1fr]">
          <div className="flex max-h-[70vh] flex-col gap-2 overflow-y-auto pr-1">
            {items.map((t) => {
              const meta = STATUS_META[t.status];
              const prio = PRIORITY_META[t.priority] || PRIORITY_META.normal;
              return (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setSelected(t.id)}
                  className={clsx(
                    'rounded-xl border p-3 text-left transition-colors',
                    t.id === selected
                      ? 'border-primary-200 bg-primary-50/40 shadow-sm'
                      : 'border-slate-200 bg-white hover:border-slate-300',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono text-[11px] text-slate-500">{t.ticket_no}</span>
                    <Tag tone={meta.tone}>{meta.label}</Tag>
                  </div>
                  <div className="mt-1 flex items-center gap-1.5">
                    <span className="text-sm font-medium text-slate-900">患者 #{t.patient_id}</span>
                    {prio !== PRIORITY_META.normal && <Tag tone={prio.tone}>{prio.label}</Tag>}
                  </div>
                  <div className="mt-1 line-clamp-2 text-[11px] leading-snug text-slate-500">
                    {t.summary || REASON_LABEL[t.reason] || t.reason}
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 text-[10px] text-slate-400">
                    <Clock className="h-3 w-3" />
                    {t.created_at.slice(5, 16)}
                    {t.message_count > 0 && <span>· {t.message_count} 条回复</span>}
                    {t.assigned_name && <span>· {t.assigned_name}</span>}
                  </div>
                </button>
              );
            })}
          </div>

          {current && (
            <Card className="flex min-h-[60vh] flex-col p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">{current.ticket_no}</span>
                    <Tag tone={STATUS_META[current.status].tone}>{STATUS_META[current.status].label}</Tag>
                    {current.assigned_name && <Tag tone="blue">认领人 {current.assigned_name}</Tag>}
                  </div>
                  <div className="mt-1 text-[11px] text-slate-500">
                    患者 #{current.patient_id} · 会话 {current.session_id} · {REASON_LABEL[current.reason] || current.reason}
                  </div>
                </div>
                {current.status === 'open' && (
                  <Button
                    size="sm"
                    disabled={busy === 'claim'}
                    onClick={() => void afterAction(() => handoffApi.claim(current.id), 'claim')}
                  >
                    <UserCheck className="h-3.5 w-3.5" />
                    {busy === 'claim' ? '认领中…' : '认领这张单'}
                  </Button>
                )}
              </div>

              {current.summary && (
                <div className="mt-2 rounded-lg bg-slate-50 px-3 py-2 text-[12px] leading-relaxed text-slate-600">
                  {current.summary}
                </div>
              )}

              <div className="mt-3">
                <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-slate-500">
                  <Sparkles className="h-3.5 w-3.5 text-primary-500" />
                  接管链路
                  <span className="font-normal text-slate-400">
                    （灰虚线=这一步不适用；蓝框=当前这一步）
                  </span>
                </div>
                <div className="overflow-x-auto pb-1">
                  <ChainView nodes={chain} />
                </div>
              </div>

              <div className="mt-4 flex items-center gap-1.5 text-[11px] font-medium text-slate-500">
                <MessageSquare className="h-3.5 w-3.5" />
                会话记录
                <span className="font-normal text-slate-400">患者说的话和坐席回复同一条时间线</span>
              </div>
              <div className="mt-2 flex max-h-[38vh] flex-1 flex-col gap-1.5 overflow-y-auto rounded-lg bg-canvas p-3">
                {msgData === null ? (
                  <div className="py-6 text-center text-[11px] text-slate-400">加载会话记录…</div>
                ) : messages.length === 0 ? (
                  <div className="py-6 text-center text-[11px] text-slate-400">
                    这张单对应的会话还没有落库消息
                  </div>
                ) : (
                  messages.map((m) => <MessageBubble key={m.seq} m={m} />)
                )}
              </div>

              {current.status === 'claimed' && (
                <div className="mt-3 flex flex-col gap-2 border-t border-slate-100 pt-3">
                  <textarea
                    value={reply}
                    onChange={(e) => setReply(e.target.value)}
                    rows={2}
                    placeholder="回复患者（会直接出现在他的会话里）"
                    className="w-full resize-y rounded-lg border border-slate-200 px-3 py-2 text-xs leading-relaxed outline-none focus:border-primary-300 focus:ring-2 focus:ring-primary-500/20"
                  />
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      disabled={!reply.trim() || busy === 'reply'}
                      onClick={() =>
                        void afterAction(async () => {
                          await handoffApi.reply(current.id, reply.trim());
                          setReply('');
                        }, 'reply')
                      }
                    >
                      <Send className="h-3.5 w-3.5" />
                      {busy === 'reply' ? '发送中…' : '发送回复'}
                    </Button>
                    <span className="text-[10px] text-slate-400">只有认领人本人能回复；单仍在办时会一直跟着这张单。</span>
                  </div>

                  <div className="mt-1 border-t border-dashed border-slate-100 pt-2">
                    <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium text-slate-500">
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                      结单
                      <span className="font-normal text-slate-400">处理说明只做站内记录，不会发给患者</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <input
                        value={resolution}
                        onChange={(e) => setResolution(e.target.value)}
                        placeholder="处理说明，例如：已电话回访，建议饭后服用，两周后复诊"
                        className="flex-1 rounded-lg border border-slate-200 px-3 py-1.5 text-xs outline-none focus:border-primary-300 focus:ring-2 focus:ring-primary-500/20"
                      />
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={!resolution.trim() || busy === 'resolve'}
                        onClick={() =>
                          void afterAction(async () => {
                            await handoffApi.resolve(current.id, resolution.trim());
                            setResolution('');
                          }, 'resolve')
                        }
                      >
                        {busy === 'resolve' ? '结单中…' : '确认结单'}
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              {current.status !== 'claimed' && current.status !== 'open' && (
                <div className="mt-3 border-t border-slate-100 pt-2 text-[11px] text-slate-400">
                  {current.status === 'resolved' || current.status === 'closed'
                    ? `已结单：${current.resolution || '（未填写说明）'} · ${current.resolved_at || '—'}`
                    : '患者已撤回，挂起的那一轮已放掉，无需再处理。'}
                </div>
              )}
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
