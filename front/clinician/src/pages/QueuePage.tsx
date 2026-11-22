import { useState } from 'react';
import dayjs from 'dayjs';
import { CheckCircle2, ClipboardList, ShieldAlert, XCircle } from 'lucide-react';

import { clinicianApi, type ClinicianPendingAction } from '@his/shared';

import { usePolling } from '../hooks/usePolling';
import { useReportRefresh } from '../store/refreshClock';
import { Button, Card, EmptyState, InlineError, Modal, Tag } from '../components/ui';

const PREVIEW_LABELS: Record<string, string> = {
  department: '科室',
  doctor_name: '医生',
  date: '日期',
  time_slot: '时段',
  fee: '费用',
  diagnosis: '初步诊断',
};

function expiresIn(iso: string | null): string {
  if (!iso) return '—';
  const diff = dayjs(iso).diff(dayjs(), 'minute');
  if (diff <= 0) return '已超时';
  return `${diff} 分钟后失效`;
}

export function QueuePage() {
  const { data, error, loading, updatedAt, refresh } = usePolling(
    () => clinicianApi.pending({ limit: 50 }),
    10_000,
  );
  useReportRefresh('queue', updatedAt);
  const [target, setTarget] = useState<ClinicianPendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [result, setResult] = useState('');

  const items = data?.items ?? [];
  // 数据还没回来时不要按"只读"渲染，否则加载中会先给医生一个错误权限暗示
  const canDecide = data?.can_decide ?? true;

  const decide = async (mode: 'approve' | 'reject') => {
    if (!target) return;
    setBusy(true);
    setActionError('');
    try {
      if (mode === 'approve') {
        const resp = await clinicianApi.approve(target.id);
        const no = (resp.data?.appointment_no as string | undefined) || resp.result_ref || '';
        setResult(no ? `已确认并建单：${no}` : '已确认执行');
      } else {
        await clinicianApi.reject(target.id);
        setResult('已驳回，该动作不会再被执行');
      }
      setTarget(null);
      await refresh();
    } catch (e) {
      // 403 是业务拒绝（范围外 / 无权限），不是登录失效，不能当登出处理
      setActionError((e as Error).message || '操作失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-end justify-between gap-3">
        <div>
          <h1 className="text-base font-semibold text-slate-900">待办确认</h1>
          <p className="mt-0.5 text-xs text-slate-400">
            只展示你责任患者的待确认动作；每 10 秒自动刷新，跨医生的动作互不可见。
          </p>
        </div>
        {data && !canDecide && <Tag tone="amber">当前账号只读（无审批权限）</Tag>}
      </div>

      {error && <InlineError text={error} />}
      {actionError && <InlineError text={actionError} />}
      {result && (
        <div className="rounded-lg border border-emerald-100 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          {result}
        </div>
      )}

      {loading && items.length === 0 ? (
        <Card className="px-4 py-10 text-center text-xs text-slate-400">加载中…</Card>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<ClipboardList className="h-5 w-5" />}
            title="暂无待确认动作"
            description="患者在你责任范围内发起的高危动作（挂号、移交）会出现在这里。"
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item) => (
            <Card key={item.id} className="p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">
                      {item.patient_name}
                      <span className="ml-1.5 text-[11px] font-normal text-slate-400">
                        #{item.patient_id ?? '—'}
                      </span>
                    </span>
                    <Tag tone={item.action_type === 'handoff' ? 'violet' : 'blue'}>
                      {item.action_type === 'handoff' ? '问诊移交' : '挂号'}
                    </Tag>
                    <Tag tone="amber">{expiresIn(item.expires_at)}</Tag>
                  </div>
                  <div className="mt-1 text-sm text-slate-700">{item.summary || '（无摘要）'}</div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
                    {Object.entries(item.preview).map(([k, v]) => (
                      <span key={k}>
                        {PREVIEW_LABELS[k] || k}：{String(v)}
                      </span>
                    ))}
                  </div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    创建于 {item.created_at ? dayjs(item.created_at).format('MM-DD HH:mm') : '—'}
                    ·会话 {item.session_id || '—'}
                  </div>
                </div>

                <div className="flex shrink-0 gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={!canDecide || busy}
                    onClick={() => {
                      setActionError('');
                      setResult('');
                      setTarget(item);
                    }}
                  >
                    <ShieldAlert className="h-3.5 w-3.5" />
                    处理
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Modal
        open={target !== null}
        title="确认这道高危动作"
        onClose={() => {
          if (!busy) setTarget(null);
        }}
        footer={
          <>
            <Button
              variant="danger"
              disabled={busy}
              onClick={() => void decide('reject')}
            >
              <XCircle className="h-3.5 w-3.5" />
              驳回
            </Button>
            <Button disabled={busy} onClick={() => void decide('approve')}>
              <CheckCircle2 className="h-3.5 w-3.5" />
              {busy ? '执行中…' : '确认并执行'}
            </Button>
          </>
        }
      >
        {target && (
          <div className="flex flex-col gap-2 text-sm">
            <div className="flex items-center gap-2 text-slate-500">
              <span>患者</span>
              <span className="font-medium text-slate-900">{target.patient_name}</span>
              <span className="text-[11px] text-slate-400">#{target.patient_id ?? '—'}</span>
            </div>
            <div className="rounded-lg bg-slate-50 px-3 py-2 text-slate-700">{target.summary}</div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-slate-600">
              {Object.entries(target.preview).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2">
                  <span className="text-slate-400">{PREVIEW_LABELS[k] || k}</span>
                  <span className="font-medium">{String(v)}</span>
                </div>
              ))}
            </div>
            {actionError && <InlineError text={actionError} />}
            <p className="text-[11px] leading-relaxed text-slate-400">
              确认后会立即落库写预约并清掉患者端的待确认卡片；重复确认幂等，不会重复建单。
            </p>
          </div>
        )}
      </Modal>
    </div>
  );
}
