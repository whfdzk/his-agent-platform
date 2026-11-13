import { useEffect, useMemo, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Pencil, Plus, Settings2, Trash2, X } from 'lucide-react';
import clsx from 'clsx';
import { systemConfigApi, type SystemConfigItem, type SystemConfigUpsert } from '@his/shared';

const TYPE_LABELS = { param: '参数', switch: '开关', dict: '字典' } as const;

const emptyForm: SystemConfigUpsert = {
  key: '',
  value: '',
  config_type: 'param',
  group: 'general',
  name: '',
  description: '',
  enabled: true,
};

export function SystemConfigPage() {
  const [configs, setConfigs] = useState<SystemConfigItem[]>([]);
  const [groupFilter, setGroupFilter] = useState('all');
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; config?: SystemConfigItem } | null>(null);

  const load = () => {
    setLoading(true);
    systemConfigApi
      .list()
      .then((r) => setConfigs(r.configs))
      .catch(() => setConfigs([]))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const groups = useMemo(() => Array.from(new Set(configs.map((c) => c.group))), [configs]);
  const filtered = groupFilter === 'all' ? configs : configs.filter((c) => c.group === groupFilter);

  const toggleEnabled = async (c: SystemConfigItem) => {
    const next = !c.enabled;
    await systemConfigApi
      .update(c.id, {
        key: c.key, value: c.raw_value, config_type: c.config_type,
        group: c.group, name: c.name, description: c.description, enabled: next,
      })
      .catch((e) => alert(e.message));
    load();
  };

  const remove = async (c: SystemConfigItem) => {
    if (!confirm(`删除配置「${c.name}」？`)) return;
    await systemConfigApi.remove(c.id).catch((e) => alert(e.message));
    load();
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">系统配置</h1>
          <p className="mt-0.5 text-xs text-gray-400">运行参数、功能开关与字典配置 · 共 {configs.length} 项</p>
        </div>
        <button
          onClick={() => setEditing({ mode: 'create' })}
          className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
        >
          <Plus className="h-4 w-4" /> 新增配置
        </button>
      </div>

      {/* 分组 Tab */}
      <div className="flex flex-wrap gap-1 rounded-xl border border-gray-200 bg-white p-1">
        {['all', ...groups].map((g) => (
          <button
            key={g}
            onClick={() => setGroupFilter(g)}
            className={clsx(
              'rounded-lg px-3 py-1.5 text-sm transition-colors',
              groupFilter === g ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50',
            )}
          >
            {g === 'all' ? '全部' : g}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
          <Settings2 className="h-10 w-10 text-gray-300" />
          <div className="text-sm text-gray-400">暂无配置项</div>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((c) => (
            <div key={c.id} className="flex items-center gap-4 rounded-2xl border border-gray-100 bg-white p-4">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-gray-700">{c.name}</span>
                  <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] text-blue-600">{TYPE_LABELS[c.config_type]}</span>
                  <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-400">{c.group}</span>
                  {!c.enabled && <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-400">未生效</span>}
                </div>
                <div className="mt-0.5 truncate font-mono text-xs text-gray-400">{c.key}</div>
                {c.description && <div className="mt-1 truncate text-xs text-gray-500">{c.description}</div>}
              </div>
              {/* 值展示 / 快捷开关 */}
              <div className="shrink-0">
                {c.config_type === 'switch' ? (
                  <button
                    onClick={() => toggleEnabled(c)}
                    className={clsx(
                      'relative h-6 w-11 rounded-full transition-colors',
                      c.value === true ? 'bg-emerald-500' : 'bg-gray-200',
                    )}
                    aria-label="切换开关"
                  >
                    <span
                      className={clsx(
                        'absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all',
                        c.value === true ? 'left-[22px]' : 'left-0.5',
                      )}
                    />
                  </button>
                ) : (
                  <span className="max-w-48 truncate rounded-xl bg-gray-50 px-3 py-1.5 font-mono text-xs text-gray-600">
                    {c.config_type === 'dict'
                      ? `数组 · ${(c.value as string[]).length} 项`
                      : String(c.value) || '—'}
                  </span>
                )}
              </div>
              <div className="flex shrink-0 gap-1">
                <button
                  onClick={() => setEditing({ mode: 'edit', config: c })}
                  className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-50 hover:text-gray-600"
                  aria-label="编辑"
                >
                  <Pencil className="h-4 w-4" />
                </button>
                <button
                  onClick={() => remove(c)}
                  className="rounded-lg p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-500"
                  aria-label="删除"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <ConfigFormModal
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          load();
        }}
      />
    </div>
  );
}

function ConfigFormModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: { mode: 'create' | 'edit'; config?: SystemConfigItem } | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<SystemConfigUpsert>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (editing?.mode === 'edit' && editing.config) {
      const c = editing.config;
      setForm({
        key: c.key,
        value: c.config_type === 'dict' ? JSON.stringify(c.value) : String(c.value),
        config_type: c.config_type,
        group: c.group,
        name: c.name,
        description: c.description,
        enabled: c.enabled,
      });
    } else {
      setForm(emptyForm);
    }
    setError('');
  }, [editing]);

  if (!editing) return null;
  const isEdit = editing.mode === 'edit';

  const submit = async () => {
    if (!form.key.trim() || !form.name.trim()) {
      setError('请填写配置键与名称');
      return;
    }
    if (form.config_type === 'dict') {
      try {
        const parsed = JSON.parse(form.value || '[]');
        if (!Array.isArray(parsed)) throw new Error();
      } catch {
        setError('字典类型的值必须是合法 JSON 数组，如 ["a","b"]');
        return;
      }
    }
    setSaving(true);
    setError('');
    try {
      if (isEdit && editing.config) {
        await systemConfigApi.update(editing.config.id, form);
      } else {
        await systemConfigApi.create(form);
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
          className="max-h-[90dvh] w-full max-w-md overflow-y-auto rounded-2xl bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-medium">{isEdit ? '编辑配置' : '新增配置'}</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="space-y-3.5">
            <Field label="配置键（唯一）">
              <input
                value={form.key}
                disabled={isEdit}
                onChange={(e) => setForm((f) => ({ ...f, key: e.target.value }))}
                placeholder="如 chat.max_turns"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-sm outline-none focus:ring-2 focus:ring-primary-100 disabled:bg-gray-50 disabled:text-gray-400"
              />
            </Field>
            <Field label="名称">
              <input
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="类型">
                <select
                  value={form.config_type}
                  onChange={(e) => setForm((f) => ({ ...f, config_type: e.target.value as SystemConfigUpsert['config_type'] }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                >
                  <option value="param">参数</option>
                  <option value="switch">开关</option>
                  <option value="dict">字典</option>
                </select>
              </Field>
              <Field label="分组">
                <input
                  value={form.group}
                  onChange={(e) => setForm((f) => ({ ...f, group: e.target.value }))}
                  placeholder="general"
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
            </div>
            <Field label={form.config_type === 'switch' ? '值（true/false）' : form.config_type === 'dict' ? '值（JSON 数组）' : '值'}>
              {form.config_type === 'switch' ? (
                <select
                  value={form.value}
                  onChange={(e) => setForm((f) => ({ ...f, value: e.target.value }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                >
                  <option value="true">开启</option>
                  <option value="false">关闭</option>
                </select>
              ) : form.config_type === 'dict' ? (
                <textarea
                  value={form.value}
                  onChange={(e) => setForm((f) => ({ ...f, value: e.target.value }))}
                  rows={3}
                  placeholder='["选项1","选项2"]'
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary-100"
                />
              ) : (
                <input
                  value={form.value}
                  onChange={(e) => setForm((f) => ({ ...f, value: e.target.value }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              )}
            </Field>
            <Field label="说明">
              <input
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
                className="h-4 w-4 rounded"
              />
              启用（生效）
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-xs text-gray-500">{label}</label>
      {children}
    </div>
  );
}
