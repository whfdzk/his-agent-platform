import { useState } from 'react';
import { Users, X } from 'lucide-react';
import clsx from 'clsx';

import { clinicianApi, type ClinicianPatientDetail } from '@his/shared';

import { usePolling } from '../hooks/usePolling';
import { useReportRefresh } from '../store/refreshClock';
import { Card, EmptyState, InlineError, LevelBadge, Tag } from '../components/ui';

const LEVEL_CLASS: Record<string, string> = {
  red: 'bg-red-100 text-red-700',
  orange: 'bg-orange-100 text-orange-700',
  yellow: 'bg-amber-100 text-amber-700',
  green: 'bg-emerald-100 text-emerald-700',
};

export function PatientsPage() {
  const { data, error, loading, updatedAt } = usePolling(
    () => clinicianApi.patients({ limit: 100 }),
    30_000,
  );
  useReportRefresh('patients', updatedAt);
  const items = data?.items ?? [];

  // 档案抽屉：点卡片打开，数据按需拉一次（列表接口不带病历字段）
  const [openId, setOpenId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ClinicianPatientDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');

  async function openPatient(patientId: number) {
    setOpenId(patientId);
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      setDetail(await clinicianApi.patientDetail(patientId));
    } catch (e) {
      setDetailError(e instanceof Error ? e.message : '档案加载失败');
    } finally {
      setDetailLoading(false);
    }
  }

  function closeDrawer() {
    setOpenId(null);
    setDetail(null);
    setDetailError('');
  }

  const profile = detail?.patient;

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
            <button
              key={p.patient_id}
              type="button"
              onClick={() => void openPatient(p.patient_id)}
              className="text-left"
            >
              <Card
                className={clsx(
                  'p-3 transition-colors',
                  openId === p.patient_id
                    ? 'border-primary-200 bg-primary-50/40'
                    : 'hover:border-slate-300',
                )}
              >
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
            </button>
          ))}
        </div>
      )}

      {openId !== null && (
        <div className="fixed inset-0 z-40">
          <div className="absolute inset-0 bg-slate-900/20" onClick={closeDrawer} />
          <aside className="absolute right-0 top-0 flex h-full w-full max-w-[440px] flex-col gap-3 overflow-y-auto bg-white p-4 shadow-xl">
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-base font-semibold text-slate-900">
                    {profile?.name ?? '加载中…'}
                  </span>
                  {profile && <span className="text-[11px] text-slate-400">#{profile.patient_id}</span>}
                </div>
                {profile && (
                  <div className="mt-0.5 text-[11px] text-slate-500">
                    {[profile.gender, profile.age ? `${profile.age} 岁` : null, profile.blood_type]
                      .filter(Boolean)
                      .join(' · ') || '资料不全'}
                    {profile.phone_masked ? ` · ${profile.phone_masked}` : ''}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={closeDrawer}
                className="rounded-lg p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {detailLoading && <div className="py-8 text-center text-xs text-slate-400">档案加载中…</div>}
            {detailError && <InlineError text={detailError} />}

            {detail && (
              <>
                <div className="rounded-lg border border-slate-200 p-3">
                  <div className="mb-1 text-[11px] font-medium text-slate-500">档案</div>
                  <div className="flex flex-col gap-1 text-[12px] leading-relaxed text-slate-600">
                    <div>
                      <span className="text-slate-400">过敏史：</span>
                      {profile?.allergy_history || '未记录'}
                    </div>
                    <div>
                      <span className="text-slate-400">既往史：</span>
                      {profile?.medical_history || '未记录'}
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {detail.recent_departments.length > 0 ? (
                        detail.recent_departments.map((d) => (
                          <Tag key={d} tone="slate">
                            {d}
                          </Tag>
                        ))
                      ) : (
                        <Tag tone="slate">暂无就诊科室</Tag>
                      )}
                    </div>
                  </div>
                </div>

                <div>
                  <div className="mb-1 text-[11px] font-medium text-slate-500">
                    最近急症 <span className="font-normal text-slate-400">{detail.alerts.length} 条</span>
                  </div>
                  {detail.alerts.length === 0 ? (
                    <div className="rounded-lg bg-canvas px-3 py-4 text-center text-[11px] text-slate-400">
                      没有可查看的急症记录
                    </div>
                  ) : (
                    <div className="flex flex-col gap-1.5">
                      {detail.alerts.map((a) => (
                        <div key={a.id} className="rounded-lg border border-slate-200 px-3 py-2">
                          <div className="flex items-center justify-between gap-2">
                            <span
                              className={clsx(
                                'inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-semibold',
                                LEVEL_CLASS[a.level] || 'bg-slate-100 text-slate-600',
                              )}
                            >
                              {a.level_label || a.level}
                            </span>
                            <span className="text-[10px] text-slate-400">
                              {a.created_at ? a.created_at.slice(5, 16).replace('T', ' ') : ''}
                            </span>
                          </div>
                          <div className="mt-1 text-[11px] leading-snug text-slate-600">
                            {a.reason || '—'}
                          </div>
                          {a.symptoms.length > 0 && (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {a.symptoms.slice(0, 6).map((s) => (
                                <Tag key={s} tone="slate">
                                  {s}
                                </Tag>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div>
                  <div className="mb-1 text-[11px] font-medium text-slate-500">
                    最近问诊 <span className="font-normal text-slate-400">{detail.consultations.length} 条</span>
                  </div>
                  {detail.consultations.length === 0 ? (
                    <div className="rounded-lg bg-canvas px-3 py-4 text-center text-[11px] text-slate-400">
                      还没有问诊记录
                    </div>
                  ) : (
                    <div className="flex flex-col gap-1.5">
                      {detail.consultations.map((c) => (
                        <div key={c.id} className="rounded-lg border border-slate-200 px-3 py-2">
                          <div className="flex items-center justify-between gap-2 text-[11px]">
                            <span className="font-medium text-slate-700">{c.department || '未分科'}</span>
                            <span className="text-[10px] text-slate-400">
                              {c.created_at ? c.created_at.slice(5, 16).replace('T', ' ') : ''}
                            </span>
                          </div>
                          <div className="mt-1 text-[11px] leading-snug text-slate-600">
                            主诉：{c.chief_complaint || '—'}
                          </div>
                          {c.diagnosis && (
                            <div className="text-[11px] leading-snug text-slate-500">诊断：{c.diagnosis}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="text-[10px] text-slate-400">
                  可见原因：{detail.visible_reason}；手机号已脱敏，身份证不下发。
                </div>
              </>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
