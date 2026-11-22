import { Headset, CheckCircle2, Undo2, X } from 'lucide-react';
import clsx from 'clsx';
import type { HandoffTicket } from '@his/shared';

interface Props {
  ticket: HandoffTicket;
  busy: boolean;
  error: string;
  onCancel: () => void;
  onDismiss: () => void;
}

const STATUS_TEXT: Record<string, string> = {
  open: '正在等坐席接入',
  claimed: '坐席已接入',
  resolved: '人工环节已结束',
  closed: '人工环节已结束',
  cancelled: '已撤回人工请求',
};

/**
 * 患者端工单状态条：挂在输入框上方。
 * 患者点「转人工」后，这里是他唯一能确认"有人在管我"的地方，
 * 所以四个状态都要说清楚，不能只是转圈。
 */
export function HandoffBar({ ticket, busy, error, onCancel, onDismiss }: Props) {
  const status = ticket.status;
  const active = status === 'open' || status === 'claimed';
  const tone = active
    ? status === 'claimed'
      ? 'border-primary-200 bg-primary-50 text-primary-800'
      : 'border-amber-200 bg-amber-50 text-amber-900'
    : status === 'cancelled'
      ? 'border-slate-200 bg-slate-50 text-slate-600'
      : 'border-emerald-200 bg-emerald-50 text-emerald-800';

  return (
    <div className={clsx('mx-4 mb-2 rounded-xl border px-3 py-2.5 text-xs', tone)}>
      <div className="flex items-start gap-2">
        {active ? (
          <Headset className="mt-0.5 h-4 w-4 shrink-0" />
        ) : (
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{STATUS_TEXT[status] || status}</span>
            <span className="opacity-70">工单 {ticket.ticket_no}</span>
            {status === 'claimed' && ticket.assigned_name && (
              <span className="rounded-md bg-white/70 px-1.5 py-0.5 font-medium">
                {ticket.assigned_name}
              </span>
            )}
          </div>
          <div className="mt-1 leading-relaxed opacity-80">
            {status === 'open' &&
              '工单已进入坐席队列，接入后坐席的回复会直接出现在这个会话里，你可以继续补充情况。'}
            {status === 'claimed' &&
              '坐席会看到你在这个会话里的全部记录；回复由真人直接写下，可能与你说话有先后。'}
            {(status === 'resolved' || status === 'closed') &&
              (ticket.resolution ? '人工坐席已处理完这张工单。' : '这张工单已由坐席办结。')}
            {status === 'cancelled' && '你撤回了人工请求，AI 会继续接待你。'}
          </div>
          {error && <div className="mt-1 text-red-600">{error}</div>}
        </div>
        {status === 'open' && (
          <button
            onClick={onCancel}
            disabled={busy}
            className="flex shrink-0 items-center gap-1 rounded-lg border border-amber-300 bg-white/80 px-2 py-1 text-[11px] text-amber-800 hover:bg-white disabled:opacity-50"
            title="坐席还没接入，可以撤回"
          >
            <Undo2 className="h-3 w-3" />
            {busy ? '撤回中…' : '撤回'}
          </button>
        )}
        {!active && (
          <button
            onClick={onDismiss}
            className="shrink-0 rounded-md p-1 opacity-60 hover:bg-white/60 hover:opacity-100"
            aria-label="关闭提示"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}
