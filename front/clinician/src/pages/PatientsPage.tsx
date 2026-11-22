import { Users } from 'lucide-react';

import { clinicianApi } from '@his/shared';

import { usePolling } from '../hooks/usePolling';
import { useReportRefresh } from '../store/refreshClock';
import { Card, EmptyState, InlineError, LevelBadge, Tag } from '../components/ui';

export function PatientsPage() {
  const { data, error, loading, updatedAt } = usePolling(
    () => clinicianApi.patients({ limit: 100 }),
    30_000,
  );
  useReportRefresh('patients', updatedAt);
  const items = data?.items ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div>
        <h1 className="text-base font-semibold text-slate-900">我的患者</h1>
        <p className="mt-0.5 text-xs text-slate-400">
          责任关系决定你能看谁的全部等级记录；这里不下发手机号，需要时走档案详情。
        </p>
      </div>

      {error && <InlineError text={error} />}

      {loading && items.length === 0 ? (
        <Card className="px-4 py-10 text-center text-xs text-slate-400">加载中…</Card>
      ) : items.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Users className="h-5 w-5" />}
            title="还没有责任患者"
            description="责任关系由接诊或管理侧写入；医生不能自助认领患者，否则等于可认领任意患者。"
          />
        </Card>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {items.map((p) => (
            <Card key={p.patient_id} className="p-3">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold text-slate-900">{p.name}</span>
                    <span className="text-[11px] text-slate-400">#{p.patient_id}</span>
                  </div>
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    {[p.gender, p.age ? `${p.age} 岁` : null].filter(Boolean).join(' · ') || '资料不全'}
                  </div>
                </div>
                {p.latest_alert ? (
                  <LevelBadge level={p.latest_alert.level} label={p.latest_alert.level_label} />
                ) : (
                  <Tag tone="slate">无高危记录</Tag>
                )}
              </div>

              <div className="mt-2 flex flex-wrap gap-1">
                <Tag tone={p.relation === 'consulted' ? 'violet' : 'blue'}>
                  {p.relation === 'consulted' ? '接诊关系' : '指派关系'}
                </Tag>
                {p.recent_departments.length > 0 ? (
                  p.recent_departments.map((d) => (
                    <Tag key={d} tone="slate">
                      {d}
                    </Tag>
                  ))
                ) : (
                  <Tag tone="slate">暂无就诊科室</Tag>
                )}
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
