import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Crown, Pencil, Plus, Trash2, X } from 'lucide-react';
import clsx from 'clsx';
import {
  membershipAdminApi,
  type MembershipOrderItem,
  type MembershipPlanItem,
  type MembershipPlanUpsert,
  type UserSubscriptionItem,
} from '@his/shared';

type Tab = 'plans' | 'orders' | 'subscriptions';

const ORDER_STATUS: Record<MembershipOrderItem['status'], { label: string; cls: string }> = {
  pending: { label: '待支付', cls: 'bg-amber-50 text-amber-600' },
  paid: { label: '已支付', cls: 'bg-emerald-50 text-emerald-600' },
  cancelled: { label: '已取消', cls: 'bg-gray-100 text-gray-400' },
  refunded: { label: '已退款', cls: 'bg-red-50 text-red-500' },
};

const yuan = (cents: number) => `¥${(cents / 100).toFixed(2)}`;

export function MembersPage() {
  const [tab, setTab] = useState<Tab>('plans');
  const [plans, setPlans] = useState<MembershipPlanItem[]>([]);
  const [orders, setOrders] = useState<MembershipOrderItem[]>([]);
  const [subs, setSubs] = useState<UserSubscriptionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; plan?: MembershipPlanItem } | null>(null);
  const [creatingOrder, setCreatingOrder] = useState(false);

  const load = () => {
    setLoading(true);
    Promise.all([
      membershipAdminApi.plans().catch(() => ({ plans: [] })),
      membershipAdminApi.orders().catch(() => ({ orders: [] })),
      membershipAdminApi.subscriptions().catch(() => ({ subscriptions: [] })),
    ])
      .then(([p, o, s]) => {
        setPlans(p.plans);
        setOrders(o.orders);
        setSubs(s.subscriptions);
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const removePlan = async (p: MembershipPlanItem) => {
    if (!confirm(`删除套餐「${p.name}」？有订单引用时会自动转为下架。`)) return;
    await membershipAdminApi.removePlan(p.id).catch((e) => alert(e.message));
    load();
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">会员管理</h1>
          <p className="mt-0.5 text-xs text-gray-400">会员套餐、订单核销与权益管理（P2）</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setCreatingOrder(true)}
            className="rounded-xl border border-gray-200 bg-white px-4 py-2 text-sm text-gray-600 hover:bg-gray-50"
          >
            人工核销开通
          </button>
          <button
            onClick={() => setEditing({ mode: 'create' })}
            className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
          >
            <Plus className="h-4 w-4" /> 新增套餐
          </button>
        </div>
      </div>

      <div className="flex gap-1 rounded-xl border border-gray-200 bg-white p-1">
        {([
          ['plans', `套餐（${plans.length}）`],
          ['orders', `订单（${orders.length}）`],
          ['subscriptions', `订阅（${subs.length}）`],
        ] as const).map(([t, label]) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx(
              'flex-1 rounded-lg px-3 py-1.5 text-sm transition-colors',
              tab === t ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : tab === 'plans' ? (
        plans.length === 0 ? (
          <Empty text="暂无套餐" />
        ) : (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {plans.map((p) => (
              <div key={p.id} className={clsx('rounded-2xl border bg-white p-5', p.enabled ? 'border-gray-100' : 'border-gray-100 opacity-60')}>
                <div className="mb-3 flex items-start justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <Crown className="h-4 w-4 text-amber-500" />
                      <span className="text-sm font-medium">{p.name}</span>
                    </div>
                    <div className="mt-1 text-xl font-medium text-amber-600">{yuan(p.price_cents)}</div>
                    <div className="text-xs text-gray-400">{p.duration_days} 天有效</div>
                  </div>
                  <div className="flex gap-1">
                    <button onClick={() => setEditing({ mode: 'edit', plan: p })} className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-50 hover:text-gray-600" aria-label="编辑">
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button onClick={() => removePlan(p)} className="rounded-lg p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-500" aria-label="删除">
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                <ul className="space-y-1">
                  {p.benefits.map((b, i) => (
                    <li key={i} className="flex items-center gap-1.5 text-xs text-gray-500">
                      <span className="h-1 w-1 rounded-full bg-emerald-400" /> {b}
                    </li>
                  ))}
                  {p.benefits.length === 0 && <li className="text-xs text-gray-300">未配置权益</li>}
                </ul>
                {!p.enabled && <div className="mt-2 text-[10px] text-gray-400">已下架</div>}
              </div>
            ))}
          </div>
        )
      ) : tab === 'orders' ? (
        orders.length === 0 ? (
          <Empty text="暂无订单" />
        ) : (
          <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                  <th className="px-4 py-3 font-normal">订单号</th>
                  <th className="px-4 py-3 font-normal">用户</th>
                  <th className="px-4 py-3 font-normal">套餐</th>
                  <th className="px-4 py-3 font-normal">金额</th>
                  <th className="px-4 py-3 font-normal">状态</th>
                  <th className="px-4 py-3 font-normal">时间</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                    <td className="px-4 py-3 font-mono text-xs text-gray-500">{o.order_no}</td>
                    <td className="px-4 py-3 text-gray-500">{o.user_id}</td>
                    <td className="px-4 py-3 text-gray-700">{o.plan_name}</td>
                    <td className="px-4 py-3 text-gray-700">{yuan(o.amount_cents)}</td>
                    <td className="px-4 py-3">
                      <span className={clsx('rounded-full px-2 py-0.5 text-[10px]', ORDER_STATUS[o.status].cls)}>
                        {ORDER_STATUS[o.status].label}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-400">{o.created_at?.slice(0, 16).replace('T', ' ') || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : subs.length === 0 ? (
        <Empty text="暂无订阅" />
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
          <table className="w-full min-w-[720px] text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                <th className="px-4 py-3 font-normal">用户</th>
                <th className="px-4 py-3 font-normal">套餐</th>
                <th className="px-4 py-3 font-normal">开始</th>
                <th className="px-4 py-3 font-normal">到期</th>
                <th className="px-4 py-3 font-normal">状态</th>
              </tr>
            </thead>
            <tbody>
              {subs.map((s) => (
                <tr key={s.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-3 text-gray-500">{s.user_id}</td>
                  <td className="px-4 py-3 text-gray-700">{s.plan_name}</td>
                  <td className="px-4 py-3 text-xs text-gray-400">{s.start_at?.slice(0, 10) || '-'}</td>
                  <td className="px-4 py-3 text-xs text-gray-400">{s.expire_at?.slice(0, 10) || '-'}</td>
                  <td className="px-4 py-3">
                    <span className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px]',
                      s.status === 'active' ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                    )}>
                      {s.status === 'active' ? '生效中' : '已过期'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PlanFormModal
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          load();
        }}
      />

      <OrderModal
        plans={plans}
        open={creatingOrder}
        onClose={() => setCreatingOrder(false)}
        onSaved={() => {
          setCreatingOrder(false);
          load();
        }}
      />
    </div>
  );
}

function PlanFormModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: { mode: 'create' | 'edit'; plan?: MembershipPlanItem } | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<MembershipPlanUpsert>({ name: '', price_cents: 0, duration_days: 30, benefits: [], enabled: true, sort_order: 0 });
  const [benefitsText, setBenefitsText] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (editing?.mode === 'edit' && editing.plan) {
      const p = editing.plan;
      setForm({ name: p.name, price_cents: p.price_cents, duration_days: p.duration_days, benefits: p.benefits, enabled: p.enabled, sort_order: p.sort_order });
      setBenefitsText(p.benefits.join('\n'));
    } else {
      setForm({ name: '', price_cents: 0, duration_days: 30, benefits: [], enabled: true, sort_order: 0 });
      setBenefitsText('');
    }
    setError('');
  }, [editing]);

  if (!editing) return null;
  const isEdit = editing.mode === 'edit';

  const submit = async () => {
    if (!form.name.trim()) {
      setError('请填写套餐名称');
      return;
    }
    setSaving(true);
    setError('');
    const payload = {
      ...form,
      benefits: benefitsText.split('\n').map((s) => s.trim()).filter(Boolean),
      price_cents: Math.round(form.price_cents),
    };
    try {
      if (isEdit && editing.plan) {
        await membershipAdminApi.updatePlan(editing.plan.id, payload);
      } else {
        await membershipAdminApi.createPlan(payload);
      }
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
          className="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-medium">{isEdit ? '编辑套餐' : '新增套餐'}</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="space-y-3.5">
            <Field label="套餐名称">
              <input
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="如 月度会员"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <div className="grid grid-cols-3 gap-3">
              <Field label="价格（元）">
                <input
                  type="number"
                  min={0}
                  step="0.01"
                  value={form.price_cents / 100}
                  onChange={(e) => setForm((f) => ({ ...f, price_cents: Math.round(Number(e.target.value) * 100) }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
              <Field label="有效期（天）">
                <input
                  type="number"
                  min={1}
                  value={form.duration_days}
                  onChange={(e) => setForm((f) => ({ ...f, duration_days: Number(e.target.value) }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
              <Field label="排序">
                <input
                  type="number"
                  value={form.sort_order}
                  onChange={(e) => setForm((f) => ({ ...f, sort_order: Number(e.target.value) }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
            </div>
            <Field label="权益（每行一条）">
              <textarea
                value={benefitsText}
                onChange={(e) => setBenefitsText(e.target.value)}
                rows={4}
                placeholder={'无限次 AI 问诊\n优先预约专家号'}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input type="checkbox" checked={form.enabled} onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))} className="h-4 w-4 rounded" />
              上架
            </label>
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

function OrderModal({
  plans,
  open,
  onClose,
  onSaved,
}: {
  plans: MembershipPlanItem[];
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [userId, setUserId] = useState('');
  const [planId, setPlanId] = useState<number | ''>('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  if (!open) return null;

  const submit = async () => {
    if (!userId.trim() || !planId) {
      setError('请填写用户 ID 并选择套餐');
      return;
    }
    setSaving(true);
    setError('');
    try {
      await membershipAdminApi.createOrder({ user_id: userId.trim(), plan_id: Number(planId) });
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
          className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-medium">人工核销开通会员</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="space-y-3.5">
            <Field label="用户 ID（patients.id）">
              <input
                value={userId}
                onChange={(e) => setUserId(e.target.value)}
                placeholder="如 1"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <Field label="套餐">
              <select
                value={planId}
                onChange={(e) => setPlanId(e.target.value ? Number(e.target.value) : '')}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              >
                <option value="">请选择</option>
                {plans.map((p) => (
                  <option key={p.id} value={p.id}>{p.name}（{yuan(p.price_cents)}）</option>
                ))}
              </select>
            </Field>
            <p className="text-xs text-gray-400">核销后创建已支付订单，并为用户开通 / 顺延会员。</p>
          </div>
          {error && <div className="mt-3 text-xs text-red-500">{error}</div>}
          <button
            onClick={submit}
            disabled={saving}
            className="mt-5 w-full rounded-xl bg-primary-500 py-2.5 text-sm text-white hover:bg-primary-600 disabled:opacity-50"
          >
            {saving ? '核销中…' : '确认核销'}
          </button>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function Empty({ text }: { text: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
      <Crown className="h-10 w-10 text-gray-300" />
      <div className="text-sm text-gray-400">{text}</div>
    </div>
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
