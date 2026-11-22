import { useEffect, useState } from 'react';
import { ShieldCheck, Stethoscope, UserRoundPlus, X } from 'lucide-react';
import clsx from 'clsx';

import {
  adminClinicianApi,
  type AdminAuditEntry,
  type AdminClinicianDetail,
  type AdminClinicianItem,
} from '@his/shared';

const RELATION_LABELS: Record<string, string> = {
  assigned: '指派',
  consulted: '接诊',
};

/** 档案字段的中文名，审计里直接展示"职称 主治医师 → 副主任医师" */
const PROFILE_LABELS: Record<string, string> = {
  employee_no: '工号',
  title: '职称',
  specialty: '专长',
  intro: '简介',
  status: '出诊状态',
  note: '备注',
};

const STATUS_TEXT: Record<string, string> = {
  active: '在岗',
  inactive: '停诊',
};

export function CliniciansPage() {
  const [items, setItems] = useState<AdminClinicianItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [detailId, setDetailId] = useState<number | null>(null);
  const [grantId, setGrantId] = useState('');
  // 筛选：职称按档案里的取值动态生成，状态固定三档（含"未建档"这一非空档）
  const [titleFilter, setTitleFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  const titles = Array.from(
    new Set(items.map((i) => i.profile?.title).filter((t): t is string => Boolean(t))),
  ).sort();

  const filtered = items.filter((i) => {
    if (titleFilter && i.profile?.title !== titleFilter) return false;
    if (statusFilter === 'none' && i.profile?.title) return false;
    if ((statusFilter === 'active' || statusFilter === 'inactive') && i.profile?.status !== statusFilter) {
      return false;
    }
    return true;
  });

  const load = () => {
    setLoading(true);
    adminClinicianApi
      .list()
      .then((r) => {
        setItems(r.items);
        setError('');
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const grant = async () => {
    const id = Number(grantId);
    if (!Number.isInteger(id) || id <= 0) {
      setError('请输入有效的账号 ID（患者账号的 patients.id）');
      return;
    }
    try {
      await adminClinicianApi.grantRole(id, '管理端授予');
      setGrantId('');
      setError('');
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-medium">医护授权</h1>
        <p className="mt-0.5 text-xs text-gray-400">
          角色决定"能不能进工作台"，科室与责任患者决定"能看到谁" · 共 {items.length} 个医生账号
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-3 rounded-2xl border border-gray-100 bg-white p-4">
        <div>
          <div className="text-xs text-gray-400">给已有患者账号授予医生角色</div>
          <div className="mt-1 flex items-center gap-2">
            <input
              value={grantId}
              onChange={(e) => setGrantId(e.target.value.replace(/[^0-9]/g, ''))}
              placeholder="患者账号 ID，如 4"
              className="w-40 rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
            />
            <button
              onClick={() => void grant()}
              className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
            >
              <UserRoundPlus className="h-4 w-4" />
              授予医生角色
            </button>
          </div>
        </div>
        <p className="text-[11px] leading-relaxed text-gray-400">
          账号 ID 就是"用户管理"里的用户 ID；授权后还要绑科室或责任患者，否则该医生进工作台会被直接拒绝（403）。
        </p>
      </div>

      {error && (
        <div className="rounded-xl border border-rose-100 bg-rose-50 px-4 py-2 text-sm text-rose-600">{error}</div>
      )}

      {!loading && items.length > 0 && (
        <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-gray-100 bg-white px-4 py-3">
          <span className="text-xs text-gray-500">筛选</span>
          <select
            value={titleFilter}
            onChange={(e) => setTitleFilter(e.target.value)}
            className="rounded-xl border border-gray-200 px-3 py-1.5 text-xs outline-none focus:ring-2 focus:ring-primary-100"
          >
            <option value="">全部职称</option>
            {titles.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="rounded-xl border border-gray-200 px-3 py-1.5 text-xs outline-none focus:ring-2 focus:ring-primary-100"
          >
            <option value="">全部出诊状态</option>
            <option value="active">在岗</option>
            <option value="inactive">停诊</option>
            <option value="none">未建档</option>
          </select>
          {(titleFilter || statusFilter) && (
            <button
              onClick={() => {
                setTitleFilter('');
                setStatusFilter('');
              }}
              className="rounded-lg px-2 py-1 text-xs text-gray-400 hover:bg-gray-50 hover:text-gray-600"
            >
              清除筛选
            </button>
          )}
          <span className="ml-auto text-[11px] text-gray-400">
            匹配 {filtered.length} / {items.length}
          </span>
        </div>
      )}

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
          <Stethoscope className="h-10 w-10 text-gray-300" />
          <div className="text-sm text-gray-400">还没有医生账号，先在上面授予角色</div>
        </div>
      ) : filtered.length === 0 ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-12 text-center text-sm text-gray-400">
          当前筛选没有匹配的医生，换个职称或出诊状态试试
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
          <table className="w-full min-w-[900px] text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                <th className="px-4 py-3 font-normal">医生</th>
                <th className="px-4 py-3 font-normal">职称 / 档案</th>
                <th className="px-4 py-3 font-normal">手机号</th>
                <th className="px-4 py-3 font-normal">角色</th>
                <th className="px-4 py-3 font-normal">科室</th>
                <th className="px-4 py-3 font-normal">责任患者</th>
                <th className="px-4 py-3 font-normal">代批</th>
                <th className="px-4 py-3 font-normal">账号状态</th>
                <th className="px-4 py-3 text-right font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((it) => (
                <tr key={it.clinician_id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    <button
                      onClick={() => setDetailId(it.clinician_id)}
                      className="font-medium text-gray-700 hover:text-primary-600"
                    >
                      {it.name}
                    </button>
                    <span className="ml-1.5 text-[11px] text-gray-400">#{it.clinician_id}</span>
                  </td>
                  <td className="px-4 py-3">
                    {it.profile?.title ? (
                      <div className="flex flex-wrap items-center gap-1">
                        <span className="text-gray-600">{it.profile.title}</span>
                        <span
                          className={clsx(
                            'rounded-full px-2 py-0.5 text-[10px]',
                            it.profile.status === 'inactive'
                              ? 'bg-amber-50 text-amber-600'
                              : 'bg-emerald-50 text-emerald-600',
                          )}
                        >
                          {it.profile.status === 'inactive' ? '停诊' : '在岗'}
                        </span>
                      </div>
                    ) : (
                      <span className="text-[11px] text-amber-600">未建档（患者端挂不到他）</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{it.phone || '-'}</td>
                  <td className="px-4 py-3 text-gray-500">{it.roles.join(' / ') || '-'}</td>
                  <td className="px-4 py-3">
                    {it.departments.length === 0 ? (
                      <span className="text-[11px] text-amber-600">未绑定</span>
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {it.departments.map((d) => (
                          <span
                            key={d.id}
                            className={clsx(
                              'rounded-full px-2 py-0.5 text-[10px]',
                              d.is_primary ? 'bg-primary-50 text-primary-700' : 'bg-gray-100 text-gray-500',
                            )}
                          >
                            {d.name}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500">{it.patient_count}</td>
                  <td className="px-4 py-3">
                    <span
                      className={clsx(
                        'rounded-full px-2 py-0.5 text-[10px]',
                        it.can_decide ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-500',
                      )}
                    >
                      {it.can_decide ? '可代批' : '只读'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={clsx(
                        'rounded-full px-2 py-0.5 text-[10px]',
                        it.enabled ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                      )}
                    >
                      {it.enabled ? '已启用' : '已停用'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => setDetailId(it.clinician_id)}
                      className="rounded-lg px-3 py-1.5 text-xs text-primary-600 hover:bg-primary-50"
                    >
                      管理范围
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detailId !== null && (
        <ClinicianDrawer
          clinicianId={detailId}
          onClose={() => setDetailId(null)}
          onChanged={load}
        />
      )}
    </div>
  );
}

function ClinicianDrawer({
  clinicianId,
  onClose,
  onChanged,
}: {
  clinicianId: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<AdminClinicianDetail | null>(null);
  const [audits, setAudits] = useState<AdminAuditEntry[]>([]);
  const [depts, setDepts] = useState<{ id: number; name: string }[]>([]);
  const [deptId, setDeptId] = useState('');
  const [isPrimary, setIsPrimary] = useState(false);
  const [patientId, setPatientId] = useState('');
  const [relation, setRelation] = useState('assigned');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [profileForm, setProfileForm] = useState({
    employee_no: '',
    title: '',
    specialty: '',
    intro: '',
    status: 'active',
  });

  const reload = () => {
    adminClinicianApi
      .get(clinicianId)
      .then((d) => {
        setDetail(d);
        // 表单跟服务端对齐：保存成功后 reload 会把输入框刷成落库后的值
        setProfileForm({
          employee_no: d.profile?.employee_no || '',
          title: d.profile?.title || '',
          specialty: d.profile?.specialty || '',
          intro: d.profile?.intro || '',
          status: d.profile?.status || 'active',
        });
        setError('');
      })
      .catch((e: Error) => setError(e.message));
    adminClinicianApi
      .audit(clinicianId, 10)
      .then((r) => setAudits(r.items))
      .catch(() => setAudits([]));
  };

  useEffect(() => {
    reload();
    adminClinicianApi
      .options()
      .then((o) => setDepts(o.departments))
      .catch(() => setDepts([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clinicianId]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      reload();
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const addDept = () => {
    const id = Number(deptId);
    if (!Number.isInteger(id) || id <= 0) {
      setError('请选择科室');
      return;
    }
    void run(() => adminClinicianApi.bindDepartment(clinicianId, id, isPrimary));
  };

  const addPatient = () => {
    const id = Number(patientId);
    if (!Number.isInteger(id) || id <= 0) {
      setError('请输入患者 ID');
      return;
    }
    void run(() => adminClinicianApi.bindPatient(clinicianId, id, relation));
    setPatientId('');
  };

  return (
    <div className="fixed inset-0 z-40 flex">
      <button className="flex-1 bg-slate-950/30" onClick={onClose} aria-label="关闭" />
      <aside className="flex h-full w-[min(30rem,calc(100vw-3rem))] flex-col border-l border-gray-200 bg-white shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4">
          <div>
            <div className="flex items-center gap-2 text-sm font-medium">
              <ShieldCheck className="h-4 w-4 text-primary-600" />
              {detail?.name || `账号 #${clinicianId}`}
              <span className="text-[11px] font-normal text-gray-400">#{clinicianId}</span>
            </div>
            <div className="mt-1 text-[11px] text-gray-400">
              {detail ? `角色 ${detail.roles.join(' / ') || '-'} · ${detail.can_decide ? '可代批' : '只读'}` : '加载中…'}
            </div>
          </div>
          <button onClick={onClose} className="rounded-lg p-2 text-gray-400 hover:bg-gray-100">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-4">
          {error && (
            <div className="rounded-xl border border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-600">{error}</div>
          )}

          <section>
            <div className="text-xs font-medium text-gray-700">医生档案</div>
            <p className="mt-0.5 text-[11px] text-gray-400">
              决定"患者能不能挂到这位医生"。没有档案的医生不会出现在挂号目录里；停诊只影响目录，
              不影响账号登录与历史患者。
            </p>
            <div className="mt-2 grid grid-cols-2 gap-2">
              <input
                value={profileForm.title}
                onChange={(e) => setProfileForm((f) => ({ ...f, title: e.target.value }))}
                placeholder="职称，如 主治医师"
                className="rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
              <input
                value={profileForm.employee_no}
                onChange={(e) => setProfileForm((f) => ({ ...f, employee_no: e.target.value }))}
                placeholder="工号，如 D004"
                className="rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
              <input
                value={profileForm.specialty}
                onChange={(e) => setProfileForm((f) => ({ ...f, specialty: e.target.value }))}
                placeholder="专长，如 冠心病介入"
                className="col-span-2 rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
              <input
                value={profileForm.intro}
                onChange={(e) => setProfileForm((f) => ({ ...f, intro: e.target.value }))}
                placeholder="一句话简介（患者端选医生时展示）"
                className="col-span-2 rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
              <select
                value={profileForm.status}
                onChange={(e) => setProfileForm((f) => ({ ...f, status: e.target.value }))}
                className="rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              >
                <option value="active">在岗（可被挂号）</option>
                <option value="inactive">停诊（从目录移除）</option>
              </select>
              <button
                disabled={busy}
                onClick={() =>
                  void run(() =>
                    adminClinicianApi.updateProfile(clinicianId, {
                      employee_no: profileForm.employee_no,
                      title: profileForm.title,
                      specialty: profileForm.specialty,
                      intro: profileForm.intro,
                      status: profileForm.status,
                    }),
                  )
                }
                className="rounded-xl bg-gray-900 px-3 py-2 text-xs text-white hover:bg-gray-800 disabled:opacity-50"
              >
                保存档案
              </button>
            </div>
          </section>

          <section>
            <div className="flex items-center justify-between">
              <div className="text-xs font-medium text-gray-700">档案变更记录</div>
              <span className="text-[11px] text-gray-400">最近 {audits.length} 条</span>
            </div>
            <p className="mt-0.5 text-[11px] text-gray-400">
              每次保存档案都会留一条：谁改的、改前改后是什么。改停诊会影响患者挂号，出问题按这里回溯。
            </p>
            <div className="mt-2 divide-y divide-gray-50 rounded-xl border border-gray-100">
              {audits.length === 0 ? (
                <div className="px-3 py-3 text-[11px] text-gray-400">还没有变更记录</div>
              ) : (
                audits.map((a) => (
                  <div key={a.id} className="px-3 py-2 text-[11px]">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-gray-400">{a.created_at}</span>
                      <span className="text-gray-600">{a.actor_name || a.actor || '-'}</span>
                      {a.result === 'ok' ? (
                        <span className="rounded-full bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-600">
                          已保存
                        </span>
                      ) : (
                        <span className="rounded-full bg-rose-50 px-1.5 py-0.5 text-[10px] text-rose-600">
                          被拒绝
                        </span>
                      )}
                    </div>
                    {a.result === 'ok' && a.changed?.length ? (
                      <div className="mt-0.5 text-gray-600">
                        {a.changed.map((k) => (
                          <span key={k} className="mr-2 inline-block">
                            {PROFILE_LABELS[k] || k}：
                            <span className="text-gray-400 line-through">{String(a.before?.[k as keyof typeof a.before] ?? '空')}</span>
                            {' → '}
                            <span className="text-gray-800">
                              {k === 'status'
                                ? STATUS_TEXT[String(a.after?.[k as keyof typeof a.after] ?? '')] || String(a.after?.[k as keyof typeof a.after] ?? '')
                                : String(a.after?.[k as keyof typeof a.after] ?? '空') || '空'}
                            </span>
                          </span>
                        ))}
                      </div>
                    ) : (
                      <div className="mt-0.5 text-gray-500">{a.note || '未产生字段变化'}</div>
                    )}
                  </div>
                ))
              )}
            </div>
          </section>

          <section>
            <div className="text-xs font-medium text-gray-700">绑定科室</div>
            <p className="mt-0.5 text-[11px] text-gray-400">本科室内 red/orange 急症会出现在他的看板上。</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <select
                value={deptId}
                onChange={(e) => setDeptId(e.target.value)}
                className="rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              >
                <option value="">选择科室</option>
                {depts.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}（#{d.id}）
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1.5 text-xs text-gray-500">
                <input type="checkbox" checked={isPrimary} onChange={(e) => setIsPrimary(e.target.checked)} />
                设为主科室
              </label>
              <button
                disabled={busy}
                onClick={addDept}
                className="rounded-xl bg-gray-900 px-3 py-2 text-xs text-white hover:bg-gray-800 disabled:opacity-50"
              >
                绑定
              </button>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {detail?.departments.length ? (
                detail.departments.map((d) => (
                  <span
                    key={d.id}
                    className="flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-1 text-[11px] text-gray-600"
                  >
                    {d.name}
                    {d.is_primary && <span className="text-primary-600">主</span>}
                    <button
                      disabled={busy}
                      onClick={() => void run(() => adminClinicianApi.unbindDepartment(clinicianId, d.id))}
                      className="text-gray-400 hover:text-rose-500"
                      aria-label={`解绑 ${d.name}`}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))
              ) : (
                <span className="text-[11px] text-gray-400">还没有绑定科室</span>
              )}
            </div>
          </section>

          <section>
            <div className="text-xs font-medium text-gray-700">责任患者</div>
            <p className="mt-0.5 text-[11px] text-gray-400">
              责任患者的全等级记录可见，且只有责任患者能被他代批。
            </p>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <input
                value={patientId}
                onChange={(e) => setPatientId(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="患者 ID"
                className="w-28 rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
              <select
                value={relation}
                onChange={(e) => setRelation(e.target.value)}
                className="rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              >
                {Object.entries(RELATION_LABELS).map(([k, v]) => (
                  <option key={k} value={k}>
                    {v}
                  </option>
                ))}
              </select>
              <button
                disabled={busy}
                onClick={addPatient}
                className="rounded-xl bg-gray-900 px-3 py-2 text-xs text-white hover:bg-gray-800 disabled:opacity-50"
              >
                添加
              </button>
            </div>
            <div className="mt-2 divide-y divide-gray-50 rounded-xl border border-gray-100">
              {detail?.patients.length ? (
                detail.patients.map((p) => (
                  <div key={p.patient_id} className="flex items-center justify-between px-3 py-2 text-xs">
                    <span className="text-gray-700">
                      {p.name}
                      <span className="ml-1.5 text-[11px] text-gray-400">#{p.patient_id}</span>
                      <span className="ml-2 rounded-full bg-primary-50 px-1.5 py-0.5 text-[10px] text-primary-700">
                        {RELATION_LABELS[p.relation] || p.relation}
                      </span>
                    </span>
                    <button
                      disabled={busy}
                      onClick={() => void run(() => adminClinicianApi.unbindPatient(clinicianId, p.patient_id))}
                      className="text-gray-400 hover:text-rose-500"
                      aria-label={`解绑患者 ${p.name}`}
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  </div>
                ))
              ) : (
                <div className="px-3 py-3 text-[11px] text-gray-400">还没有责任患者</div>
              )}
            </div>
          </section>

          <section className="rounded-xl border border-rose-100 bg-rose-50/60 p-3">
            <div className="text-xs font-medium text-rose-700">回收医生角色</div>
            <p className="mt-0.5 text-[11px] leading-relaxed text-rose-600/80">
              会同时清空该账号的科室绑定与责任患者关系，避免下次授权时旧范围被悄悄带回来。
            </p>
            <button
              disabled={busy}
              onClick={() => {
                if (!confirm(`确定回收账号 #${clinicianId} 的医生角色并清空全部绑定？`)) return;
                void run(async () => {
                  await adminClinicianApi.revokeRole(clinicianId);
                  onClose();
                });
              }}
              className="mt-2 rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-xs text-rose-600 hover:bg-rose-50 disabled:opacity-50"
            >
              回收角色
            </button>
          </section>
        </div>
      </aside>
    </div>
  );
}
