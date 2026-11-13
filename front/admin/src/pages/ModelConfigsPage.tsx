import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Bot, Pencil, Plus, Star, Trash2, X } from 'lucide-react';
import clsx from 'clsx';
import {
  modelConfigApi,
  MODEL_TYPE_LABELS,
  type FailoverChainItem,
  type ModelConfig,
  type ModelConfigUpsert,
  type ModelType,
} from '@his/shared';

const TYPES: ModelType[] = ['chat', 'embedding', 'rerank', 'multimodal'];

const emptyForm: ModelConfigUpsert = {
  model_type: 'chat',
  name: '',
  base_url: '',
  api_key: '',
  model_name: '',
  enabled: true,
  is_default: false,
  priority: 100,
};

export function ModelConfigsPage() {
  const [configs, setConfigs] = useState<ModelConfig[]>([]);
  const [activeType, setActiveType] = useState<ModelType | 'all'>('all');
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; config?: ModelConfig } | null>(null);
  const [loading, setLoading] = useState(true);
  const [chain, setChain] = useState<FailoverChainItem[]>([]);

  const load = () => {
    setLoading(true);
    Promise.all([
      modelConfigApi.list(),
      modelConfigApi.chain().catch(() => ({ chain: [], total: 0 })),
    ])
      .then(([r, c]) => {
        setConfigs(r.model_configs);
        setChain(c.chain);
      })
      .catch(() => setConfigs([]))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const filtered = activeType === 'all' ? configs : configs.filter((c) => c.model_type === activeType);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-medium">模型配置</h1>
        <button
          onClick={() => setEditing({ mode: 'create' })}
          className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
        >
          <Plus className="h-4 w-4" /> 新增配置
        </button>
      </div>

      {/* 热更新说明 + 当前生效的故障转移链 */}
      <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
        <div>
          chat 类型配置保存后<strong>立即热更新</strong>：主模型调用失败时按下方顺序自动切换候选模型，
          全部失败才回退到 .env 的主模型。
        </div>
        {chain.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
            <span className="text-emerald-800">当前生效链：</span>
            {chain.map((c, i) => (
              <span key={c.order} className="flex items-center gap-1.5">
                {i > 0 && <span className="text-emerald-500">→</span>}
                <span className="rounded-full bg-white/70 px-2 py-0.5">
                  {i + 1}. {c.name}（{c.model_name}）
                </span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* 类型 Tab */}
      <div className="flex gap-1 rounded-xl border border-gray-200 bg-white p-1">
        {(['all', ...TYPES] as const).map((t) => (
          <button
            key={t}
            onClick={() => setActiveType(t)}
            className={clsx(
              'flex-1 rounded-lg px-3 py-1.5 text-sm transition-colors',
              activeType === t ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50',
            )}
          >
            {t === 'all' ? '全部' : MODEL_TYPE_LABELS[t]}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">
          加载中…
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
          <Bot className="h-10 w-10 text-gray-300" />
          <div className="text-sm text-gray-400">暂无该类型配置</div>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          {filtered.map((c) => (
            <div key={c.id} className="rounded-2xl border border-gray-100 bg-white p-5">
              <div className="mb-3 flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">{c.name}</span>
                    {c.is_default && (
                      <span className="flex items-center gap-0.5 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] text-amber-600">
                        <Star className="h-2.5 w-2.5 fill-current" /> 默认
                      </span>
                    )}
                    {!c.enabled && (
                      <span className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-400">已停用</span>
                    )}
                  </div>
                  <div className="mt-0.5 text-xs text-gray-400">
                    {MODEL_TYPE_LABELS[c.model_type]} · {c.model_name}
                    {c.model_type === 'chat' && (
                      <span className="ml-1 text-gray-400">· 优先级 {c.priority ?? 100}</span>
                    )}
                  </div>
                </div>
                <div className="flex gap-1">
                  <button
                    onClick={() => setEditing({ mode: 'edit', config: c })}
                    className="rounded-lg p-1.5 text-gray-400 hover:bg-gray-50 hover:text-gray-600"
                    aria-label="编辑"
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <button
                    onClick={async () => {
                      if (!confirm(`确定删除配置「${c.name}」？`)) return;
                      await modelConfigApi.remove(c.id).catch((e) => alert(e.message));
                      load();
                    }}
                    className="rounded-lg p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-500"
                    aria-label="删除"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                </div>
              </div>
              <div className="space-y-1 text-xs text-gray-500">
                <div className="truncate">Base URL：{c.base_url || '-'}</div>
                <div>API Key：{c.has_api_key ? `${c.api_key}（已设置）` : '未设置'}</div>
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
  editing: { mode: 'create' | 'edit'; config?: ModelConfig } | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<ModelConfigUpsert>(emptyForm);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (editing?.mode === 'edit' && editing.config) {
      const c = editing.config;
      setForm({
        model_type: c.model_type,
        name: c.name,
        base_url: c.base_url,
        api_key: '', // 编辑时留空 = 不修改
        model_name: c.model_name,
        enabled: c.enabled,
        is_default: c.is_default,
        priority: c.priority ?? 100,
      });
    } else {
      setForm(emptyForm);
    }
    setError('');
  }, [editing]);

  if (!editing) return null;

  const isEdit = editing.mode === 'edit';

  const submit = async () => {
    if (!form.name.trim() || !form.model_name.trim()) {
      setError('请填写展示名称与模型名称');
      return;
    }
    if (form.base_url && !/^https?:\/\//.test(form.base_url)) {
      setError('Base URL 需以 http:// 或 https:// 开头');
      return;
    }
    if (!isEdit && !form.api_key?.trim()) {
      setError('新增配置需填写 API Key');
      return;
    }
    setSaving(true);
    setError('');
    try {
      if (isEdit && editing.config) {
        await modelConfigApi.update(editing.config.id, form);
      } else {
        await modelConfigApi.create(form);
      }
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const set = <K extends keyof ModelConfigUpsert>(key: K, value: ModelConfigUpsert[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

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
            <h3 className="text-base font-medium">{isEdit ? '编辑模型配置' : '新增模型配置'}</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>

          <div className="space-y-3.5">
            <Field label="模型类型">
              <select
                value={form.model_type}
                onChange={(e) => set('model_type', e.target.value as ModelType)}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              >
                {TYPES.map((t) => (
                  <option key={t} value={t}>{MODEL_TYPE_LABELS[t]}</option>
                ))}
              </select>
            </Field>

            <Field label="展示名称">
              <input
                value={form.name}
                onChange={(e) => set('name', e.target.value)}
                placeholder="如：DeepSeek 主聊天模型"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>

            <Field label="Base URL">
              <input
                value={form.base_url}
                onChange={(e) => set('base_url', e.target.value)}
                placeholder="https://api.deepseek.com/v1"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>

            <Field label={isEdit ? 'API Key（留空表示不修改）' : 'API Key'}>
              <input
                type="password"
                value={form.api_key}
                onChange={(e) => set('api_key', e.target.value)}
                placeholder={isEdit ? '••••••••' : 'sk-...'}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>

            <Field label="模型名称">
              <input
                value={form.model_name}
                onChange={(e) => set('model_name', e.target.value)}
                placeholder="如 deepseek-chat / Qwen3-Embedding-0.6B"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>

            {form.model_type === 'chat' && (
              <Field label="故障转移优先级（数字越小越先尝试；主模型始终最前）">
                <input
                  type="number"
                  min={0}
                  max={9999}
                  value={form.priority ?? 100}
                  onChange={(e) => set('priority', Number(e.target.value))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
            )}

            <div className="flex items-center gap-6">
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => set('enabled', e.target.checked)}
                  className="h-4 w-4 rounded"
                />
                启用
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-600">
                <input
                  type="checkbox"
                  checked={form.is_default}
                  onChange={(e) => set('is_default', e.target.checked)}
                  className="h-4 w-4 rounded"
                />
                设为该类型默认
              </label>
            </div>
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
