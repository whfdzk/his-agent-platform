import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Ban, CheckCircle2, Eye, Search, Users, X } from 'lucide-react';
import clsx from 'clsx';
import {
  adminUserApi,
  type AdminUserDetail,
  type AdminUserItem,
  type AdminUserUpdate,
} from '@his/shared';

export function UsersPage() {
  const [users, setUsers] = useState<AdminUserItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'enabled' | 'disabled'>('all');
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<AdminUserItem | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);

  const load = (p = page, kw = keyword, st = statusFilter) => {
    setLoading(true);
    adminUserApi
      .list({
        keyword: kw || undefined,
        enabled: st === 'all' ? undefined : st === 'enabled',
        page: p,
        page_size: 20,
      })
      .then((r) => {
        setUsers(r.users);
        setTotal(r.total);
        setPage(p);
      })
      .catch(() => setUsers([]))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load(1, keyword, statusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  const totalPages = Math.max(Math.ceil(total / 20), 1);

  const toggleStatus = async (u: AdminUserItem) => {
    const next = !u.enabled;
    if (!confirm(`确定${next ? '启用' : '禁用'}用户「${u.name}」？禁用后其无法登录患者端。`)) return;
    await adminUserApi.updateStatus(u.id, next).catch((e) => alert(e.message));
    load();
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-medium">用户管理</h1>
          <p className="mt-0.5 text-xs text-gray-400">患者基本资料维护与账号状态管理 · 共 {total} 人</p>
        </div>
      </div>

      {/* 筛选栏 */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-full sm:w-auto">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-300" />
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && load(1)}
            placeholder="搜索姓名 / 手机号"
            className="w-full rounded-xl border border-gray-200 bg-white py-2 pl-9 pr-3 text-sm outline-none focus:ring-2 focus:ring-primary-100 sm:w-64"
          />
        </div>
        <div className="flex gap-1 rounded-xl border border-gray-200 bg-white p-1">
          {(['all', 'enabled', 'disabled'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setStatusFilter(s)}
              className={clsx(
                'rounded-lg px-3 py-1.5 text-sm transition-colors',
                statusFilter === s ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50',
              )}
            >
              {s === 'all' ? '全部' : s === 'enabled' ? '已启用' : '已禁用'}
            </button>
          ))}
        </div>
        <button
          onClick={() => load(1)}
          className="rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
        >
          查询
        </button>
      </div>

      {/* 列表 */}
      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : users.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
          <Users className="h-10 w-10 text-gray-300" />
          <div className="text-sm text-gray-400">暂无用户</div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
          <table className="w-full min-w-[880px] text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                <th className="px-4 py-3 font-normal">用户</th>
                <th className="px-4 py-3 font-normal">性别 / 年龄</th>
                <th className="px-4 py-3 font-normal">手机号</th>
                <th className="px-4 py-3 font-normal">会话 / 预约</th>
                <th className="px-4 py-3 font-normal">注册状态</th>
                <th className="px-4 py-3 font-normal">账号状态</th>
                <th className="px-4 py-3 font-normal">注册时间</th>
                <th className="px-4 py-3 text-right font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    <button onClick={() => setDetailId(u.id)} className="font-medium text-gray-700 hover:text-primary-600">
                      {u.name}
                    </button>
                  </td>
                  <td className="px-4 py-3 text-gray-500">{u.gender || '-'} / {u.age ?? '-'}</td>
                  <td className="px-4 py-3 text-gray-500">{u.phone || '-'}</td>
                  <td className="px-4 py-3 text-gray-500">{u.conversations} / {u.appointments}</td>
                  <td className="px-4 py-3">
                    <span className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px]',
                      u.registered ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                    )}>
                      {u.registered ? '已注册' : '未注册'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px]',
                      u.enabled ? 'bg-blue-50 text-blue-600' : 'bg-red-50 text-red-500',
                    )}>
                      {u.enabled ? '正常' : '已禁用'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-400">{u.created_at?.slice(0, 10) || '-'}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <button
                        onClick={() => setDetailId(u.id)}
                        className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                      >
                        <Eye className="h-3 w-3" /> 详情
                      </button>
                      <button
                        onClick={() => setEditing(u)}
                        className="rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                      >
                        编辑资料
                      </button>
                      <button
                        onClick={() => toggleStatus(u)}
                        className={clsx(
                          'flex items-center gap-1 rounded-lg px-2 py-1 text-xs',
                          u.enabled ? 'text-red-500 hover:bg-red-50' : 'text-emerald-600 hover:bg-emerald-50',
                        )}
                      >
                        {u.enabled ? <Ban className="h-3 w-3" /> : <CheckCircle2 className="h-3 w-3" />}
                        {u.enabled ? '禁用' : '启用'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 分页 */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 text-sm">
          <button
            disabled={page <= 1}
            onClick={() => load(page - 1)}
            className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 disabled:opacity-40"
          >
            上一页
          </button>
          <span className="text-gray-400">{page} / {totalPages}</span>
          <button
            disabled={page >= totalPages}
            onClick={() => load(page + 1)}
            className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}

      <UserEditModal
        user={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          load();
        }}
      />

      <UserDetailDrawer
        userId={detailId}
        onClose={() => setDetailId(null)}
        onEdit={(u) => {
          setDetailId(null);
          setEditing(u);
        }}
      />
    </div>
  );
}

const SMOKING_LABELS: Record<string, string> = { never: '从不', former: '已戒', current: '吸烟中' };
const DRINKING_LABELS: Record<string, string> = { never: '从不', occasional: '偶尔', regular: '经常' };

function UserDetailDrawer({
  userId,
  onClose,
  onEdit,
}: {
  userId: number | null;
  onClose: () => void;
  onEdit: (user: AdminUserItem) => void;
}) {
  const [detail, setDetail] = useState<AdminUserDetail | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (userId === null) {
      setDetail(null);
      setError('');
      return;
    }
    setDetail(null);
    adminUserApi
      .get(userId)
      .then(setDetail)
      .catch((e) => setError((e as Error).message));
  }, [userId]);

  if (userId === null) return null;

  const u = detail?.user;
  const profile = detail?.profile;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex justify-end bg-black/40"
        onClick={onClose}
      >
        <motion.div
          initial={{ x: 80 }}
          animate={{ x: 0 }}
          className="flex h-full w-full max-w-xl flex-col bg-white shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-start justify-between border-b border-gray-100 px-6 py-4">
            <div className="min-w-0">
              <h3 className="text-base font-medium">{u ? `${u.name} 的患者详情` : '患者详情'}</h3>
              <div className="mt-0.5 text-xs text-gray-400">
                {u ? `ID ${u.id} · ${u.phone || '未绑定手机号'} · 注册于 ${u.created_at?.slice(0, 10) || '-'}` : '加载中…'}
              </div>
            </div>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>

          <div className="flex-1 space-y-5 overflow-y-auto p-6">
            {error && <div className="rounded-xl bg-red-50 px-3 py-2 text-xs text-red-600">{error}</div>}
            {!detail && !error && <div className="text-sm text-gray-400">加载中…</div>}

            {u && (
              <>
                {/* 账号状态 */}
                <div className="flex flex-wrap items-center gap-2">
                  <span className={clsx(
                    'rounded-full px-2.5 py-1 text-[11px]',
                    u.enabled ? 'bg-emerald-50 text-emerald-600' : 'bg-red-50 text-red-500',
                  )}>
                    账号 {u.enabled ? '正常' : '已禁用'}
                  </span>
                  <span className={clsx(
                    'rounded-full px-2.5 py-1 text-[11px]',
                    u.registered ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-400',
                  )}>
                    {u.registered ? '已注册登录' : '未注册登录'}
                  </span>
                  <span className="rounded-full bg-gray-100 px-2.5 py-1 text-[11px] text-gray-500">
                    会话 {u.conversations} · 预约 {u.appointments}
                  </span>
                  {detail.membership && (
                    <span className="rounded-full bg-amber-50 px-2.5 py-1 text-[11px] text-amber-600">
                      {detail.membership.plan_name} · {detail.membership.status === 'active' ? '生效中' : '已过期'}
                    </span>
                  )}
                </div>

                {/* 基本资料 */}
                <Section title="基本资料">
                  <Grid>
                    <Item label="姓名" value={u.name} />
                    <Item label="性别" value={u.gender || '-'} />
                    <Item label="年龄" value={u.age ?? '-'} />
                    <Item label="血型" value={u.blood_type || '-'} />
                    <Item label="身份证号" value={u.id_card || '-'} />
                    <Item label="手机号" value={u.phone || '-'} />
                    <Item label="过敏史" value={u.allergy_history || '无记录'} span />
                    <Item label="既往病史" value={u.medical_history || '无记录'} span />
                  </Grid>
                </Section>

                {/* 健康档案 */}
                <Section title="健康档案">
                  {profile ? (
                    <Grid>
                      <Item label="出生日期" value={profile.birth_date || '-'} />
                      <Item label="身高" value={profile.height_cm ? `${profile.height_cm} cm` : '-'} />
                      <Item label="体重" value={profile.weight_kg ? `${profile.weight_kg} kg` : '-'} />
                      <Item label="腰围" value={profile.waist_cm ? `${profile.waist_cm} cm` : '-'} />
                      <Item label="婚姻状况" value={profile.marital_status || '-'} />
                      <Item label="职业" value={profile.occupation || '-'} />
                      <Item label="常住城市" value={profile.city || '-'} />
                      <Item label="吸烟" value={profile.smoking ? SMOKING_LABELS[profile.smoking] || profile.smoking : '-'} />
                      <Item label="饮酒" value={profile.drinking ? DRINKING_LABELS[profile.drinking] || profile.drinking : '-'} />
                      <Item label="慢性病" value={profile.chronic_diseases || '无记录'} />
                      <Item label="紧急联系人" value={profile.emergency_contact_name || '-'} />
                      <Item label="紧急联系电话" value={profile.emergency_contact_phone || '-'} />
                      <Item label="手术史" value={profile.surgery_history || '无记录'} span />
                      <Item label="家族史" value={profile.family_history || '无记录'} span />
                    </Grid>
                  ) : (
                    <div className="text-xs text-gray-400">该患者尚未完善健康档案</div>
                  )}
                </Section>

                {/* 最近会话 */}
                <Section title={`最近会话（${detail.recent_conversations.length}）`}>
                  {detail.recent_conversations.length === 0 ? (
                    <div className="text-xs text-gray-400">暂无会话记录</div>
                  ) : (
                    <div className="space-y-2">
                      {detail.recent_conversations.map((c) => (
                        <div key={c.id} className="flex items-center justify-between rounded-xl bg-gray-50 px-3 py-2">
                          <div className="min-w-0">
                            <div className="truncate text-xs text-gray-700">{c.title || c.session_id}</div>
                            <div className="text-[10px] text-gray-400">{c.last_active_at?.slice(0, 16).replace('T', ' ') || '-'}</div>
                          </div>
                          <span className={clsx(
                            'shrink-0 rounded-full px-2 py-0.5 text-[10px]',
                            c.status === 'active' ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                          )}>
                            {c.status === 'active' ? '进行中' : '已关闭'}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </Section>

                {/* 最近预约 */}
                <Section title={`最近预约（${detail.recent_appointments.length}）`}>
                  {detail.recent_appointments.length === 0 ? (
                    <div className="text-xs text-gray-400">暂无预约记录</div>
                  ) : (
                    <div className="space-y-2">
                      {detail.recent_appointments.map((a) => (
                        <div key={a.id} className="flex items-center justify-between rounded-xl bg-gray-50 px-3 py-2">
                          <div className="min-w-0">
                            <div className="truncate text-xs text-gray-700">
                              {a.department || '-'} · {a.doctor_name || '-'}
                            </div>
                            <div className="text-[10px] text-gray-400">
                              {a.date || '-'} {a.time_slot} · {a.appointment_no}
                            </div>
                          </div>
                          <span className={clsx(
                            'shrink-0 rounded-full px-2 py-0.5 text-[10px]',
                            a.status === 'confirmed' ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-400',
                          )}>
                            {a.status === 'confirmed' ? '已确认' : '已取消'}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </Section>
              </>
            )}
          </div>

          {u && (
            <div className="border-t border-gray-100 px-6 py-4">
              <button
                onClick={() => onEdit(u)}
                className="w-full rounded-xl bg-primary-500 py-2.5 text-sm text-white hover:bg-primary-600"
              >
                编辑资料
              </button>
            </div>
          )}
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-2 text-xs font-medium text-gray-500">{title}</h4>
      {children}
    </div>
  );
}

function Grid({ children }: { children: React.ReactNode }) {
  return <div className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-xl bg-gray-50 p-3">{children}</div>;
}

function Item({ label, value, span }: { label: string; value: string | number; span?: boolean }) {
  return (
    <div className={clsx('text-xs', span && 'col-span-2')}>
      <span className="text-gray-400">{label}：</span>
      <span className="whitespace-pre-wrap break-words text-gray-700">{value}</span>
    </div>
  );
}

function UserEditModal({
  user,
  onClose,
  onSaved,
}: {
  user: AdminUserItem | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<AdminUserUpdate>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (user) {
      setForm({
        name: user.name,
        gender: user.gender || '',
        age: user.age ?? undefined,
        blood_type: user.blood_type || '',
        allergy_history: user.allergy_history || '',
        medical_history: user.medical_history || '',
      });
      setError('');
    }
  }, [user]);

  if (!user) return null;

  const submit = async () => {
    setSaving(true);
    setError('');
    try {
      await adminUserApi.update(user.id, {
        ...form,
        gender: form.gender || undefined,
        blood_type: form.blood_type || undefined,
      });
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-3 sm:p-6"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.94, y: 12 }}
          animate={{ scale: 1, y: 0 }}
          className="max-h-[90dvh] w-full max-w-md overflow-y-auto rounded-2xl bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-medium">编辑用户资料</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="space-y-3.5">
            <Field label="姓名">
              <input
                value={form.name || ''}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="性别">
                <select
                  value={form.gender || ''}
                  onChange={(e) => setForm((f) => ({ ...f, gender: e.target.value }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                >
                  <option value="">未知</option>
                  <option value="男">男</option>
                  <option value="女">女</option>
                </select>
              </Field>
              <Field label="年龄">
                <input
                  type="number"
                  value={form.age ?? ''}
                  onChange={(e) => setForm((f) => ({ ...f, age: e.target.value ? Number(e.target.value) : undefined }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
            </div>
            <Field label="血型">
              <input
                value={form.blood_type || ''}
                onChange={(e) => setForm((f) => ({ ...f, blood_type: e.target.value }))}
                placeholder="A / B / O / AB"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <Field label="过敏史">
              <textarea
                value={form.allergy_history || ''}
                onChange={(e) => setForm((f) => ({ ...f, allergy_history: e.target.value }))}
                rows={2}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <Field label="既往病史">
              <textarea
                value={form.medical_history || ''}
                onChange={(e) => setForm((f) => ({ ...f, medical_history: e.target.value }))}
                rows={2}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
          </div>
          {error && <div className="mt-3 text-xs text-red-500">{error}</div>}
          <button
            onClick={submit}
            disabled={saving}
            className="mt-5 w-full rounded-xl bg-primary-500 py-2.5 text-sm text-white hover:bg-primary-600 disabled:opacity-50"
          >
            {saving ? '保存中…' : '保存'}
          </button>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-xs text-gray-500">{label}</label>
      {children}
    </div>
  );
}
