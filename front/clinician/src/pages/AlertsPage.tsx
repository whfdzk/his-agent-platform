import { useEffect, useRef, useState } from 'react';
import dayjs from 'dayjs';
import { BellRing, Filter } from 'lucide-react';
import clsx from 'clsx';

import { clinicianApi, type ClinicianAlertLevel } from '@his/shared';

import { usePolling } from '../hooks/usePolling';
import { useReportRefresh } from '../store/refreshClock';
import { Card, EmptyState, InlineError, LevelBadge, Tag } from '../components/ui';

const FILTERS: { key: string; label: string }[] = [
  { key: '', label: '全部' },
  { key: 'red', label: '危急' },
  { key: 'orange', label: '紧急' },
  { key: 'yellow', label: '次紧急' },
];

export function AlertsPage() {
  const [level, setLevel] = useState('');
  const { data, error, loading, updatedAt, refresh } = usePolling(
    () => clinicianApi.alerts({ level: level || undefined, limit: 100 }),
    10_000,
  );
  useReportRefresh('alerts', updatedAt);

  // 切换等级要立刻拉一次，否则医生点完筛选要等下一个 10s 档期才看到结果，
  // 中间这段时间列表和选中的筛选条件是矛盾的（首次由 usePolling 自己触发，跳过）。
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    void refresh();
  }, [level, refresh]);

  const items = data?.items ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-base font-semibold text-slate-900">急症看板</h1>
          <p className="mt-0.5 text-xs text-slate-400">
            责任患者的全部等级可见；非责任患者只在本科室内、且为危急/紧急时出现。
          </p>
        </div>
        <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-1">
          <Filter className="ml-1 h-3.5 w-3.5 text-slate-400" />
          {FILTERS.map((f) => (
            <button
              key={f.key || 'all'}
              type="button"
              onClick={() => setLevel(f.key)}
              className={clsx(
                'rounded px-2.5 py-1 text-[11px] transition-colors',
                level === f.key
                  ? 'bg-primary-50 font-medium text-primary-700'
                  : 'text-slate-500 hover:bg-slate-50',
              )}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {error && <InlineError text={error} />}

      {loading && items.length > 0 && (
        <div className="text-[11px] text-slate-400">刷新中…</div>
      )}

      {data?.truncated && (
        <div className="rounded-lg border border-amber-100 bg-amber-50 px-3 py-2 text-[11px] leading-relaxed text-amber-700">
          候选池已打满（最近 200 条），更早的事件没有进入过滤，列表可能不是全量。
        </div>
      )}

      {loading && items.length === 0 ? (
        <Card className="px-4 py-10 text-center text-xs text-slate-400">加载中…</Card>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<BellRing className="h-5 w-5" />}
            title="当前没有急症事件"
            description="患者对话中识别到红旗症状时会落一条分级记录；green 表示无风险，不落库。"
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((item) => (
            <Card key={item.id} className="p-3">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex items-center gap-2">
                  <LevelBadge level={item.level as ClinicianAlertLevel} label={item.level_label} />
                  <span className="text-sm font-medium text-slate-900">
                    患者 #{item.patient_id ?? '—'}
                  </span>
                  <Tag tone={item.visible_reason === 'responsible' ? 'blue' : 'amber'}>
                    {item.visible_reason === 'responsible' ? '责任患者' : '本科室'}
                  </Tag>
                </div>
                <span className="text-[11px] text-slate-400">
                  {item.created_at ? dayjs(item.created_at).format('MM-DD HH:mm:ss') : '—'}
                </span>
              </div>

              <div className="mt-1.5 text-sm text-slate-700">{item.reason || '（未给出原因）'}</div>

              {item.symptoms.length > 0 && (
                <div className="mt-1.5 flex flex-wrap gap-1">
                  {item.symptoms.map((s) => (
                    <Tag key={s} tone="slate">
                      {s}
                    </Tag>
                  ))}
                </div>
              )}

              <div className="mt-1.5 text-[11px] text-slate-400">
                会话 {item.session_id || '—'} · 来源 {item.source} · 阶段 {item.thread_id ? '问诊内' : '入口'}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
