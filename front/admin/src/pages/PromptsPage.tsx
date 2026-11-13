import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Check, FileCode2, History, Pencil, Plus, X } from 'lucide-react';
import clsx from 'clsx';
import {
  promptAdminApi,
  type AdminPrompt,
  type PromptVersionItem,
} from '@his/shared';

export function PromptsPage() {
  const [prompts, setPrompts] = useState<AdminPrompt[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; prompt?: AdminPrompt } | null>(null);
  const [versionOf, setVersionOf] = useState<AdminPrompt | null>(null);

  const load = () => {
    setLoading(true);
    promptAdminApi
      .list()
      .then((r) => setPrompts(r.prompts))
      .catch(() => setPrompts([]))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">Prompt 管理</h1>
          <p className="mt-0.5 text-xs text-gray-400">提示词版本管理：历史版本不可变，激活 / 回滚只切换指针</p>
        </div>
        <button
          onClick={() => setEditing({ mode: 'create' })}
          className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
        >
          <Plus className="h-4 w-4" /> 新增 Prompt
        </button>
      </div>

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : prompts.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
          <FileCode2 className="h-10 w-10 text-gray-300" />
          <div className="text-sm text-gray-400">暂无 Prompt</div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                <th className="px-4 py-3 font-normal">名称 / 业务键</th>
                <th className="px-4 py-3 font-normal">场景</th>
                <th className="px-4 py-3 font-normal">激活版本</th>
                <th className="px-4 py-3 font-normal">状态</th>
                <th className="px-4 py-3 font-normal">更新时间</th>
                <th className="px-4 py-3 text-right font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {prompts.map((p) => (
                <tr key={p.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-700">{p.name}</div>
                    <div className="text-xs text-gray-400">{p.key}</div>
                  </td>
                  <td className="px-4 py-3 text-gray-500">{p.scene}</td>
                  <td className="px-4 py-3">
                    <span className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px]',
                      p.active_version ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                    )}>
                      {p.active_version ? `v${p.active_version}` : '未激活'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px]',
                      p.enabled ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-400',
                    )}>
                      {p.enabled ? '启用' : '停用'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-400">{p.updated_at?.slice(0, 16).replace('T', ' ') || '-'}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <button
                        onClick={() => setVersionOf(p)}
                        className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                      >
                        <History className="h-3 w-3" /> 版本
                      </button>
                      <button
                        onClick={() => setEditing({ mode: 'edit', prompt: p })}
                        className="rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                      >
                        编辑
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <PromptFormModal
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          load();
        }}
      />

      <VersionDrawer
        prompt={versionOf}
        onClose={() => setVersionOf(null)}
        onChanged={load}
      />
    </div>
  );
}

function PromptFormModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: { mode: 'create' | 'edit'; prompt?: AdminPrompt } | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState({ key: '', name: '', scene: 'agent', description: '', first_content: '', change_note: '' });
  const [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (editing?.mode === 'edit' && editing.prompt) {
      const p = editing.prompt;
      setForm({ key: p.key, name: p.name, scene: p.scene, description: p.description, first_content: '', change_note: '' });
      setEnabled(p.enabled);
    } else {
      setForm({ key: '', name: '', scene: 'agent', description: '', first_content: '', change_note: '' });
      setEnabled(true);
    }
    setError('');
  }, [editing]);

  if (!editing) return null;
  const isEdit = editing.mode === 'edit';

  const submit = async () => {
    if (!form.key.trim() || !form.name.trim()) {
      setError('请填写业务键与名称');
      return;
    }
    setSaving(true);
    setError('');
    try {
      if (isEdit && editing.prompt) {
        await promptAdminApi.update(editing.prompt.id, {
          name: form.name, scene: form.scene, description: form.description, enabled,
        });
      } else {
        await promptAdminApi.create(form);
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
            <h3 className="text-base font-medium">{isEdit ? '编辑 Prompt' : '新增 Prompt'}</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="space-y-3.5">
            <Field label="业务键（唯一）">
              <input
                value={form.key}
                disabled={isEdit}
                onChange={(e) => setForm((f) => ({ ...f, key: e.target.value }))}
                placeholder="如 supervisor.system"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100 disabled:bg-gray-50 disabled:text-gray-400"
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
              <Field label="场景">
                <select
                  value={form.scene}
                  onChange={(e) => setForm((f) => ({ ...f, scene: e.target.value }))}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                >
                  <option value="agent">agent</option>
                  <option value="tool">tool</option>
                  <option value="audit">audit</option>
                  <option value="other">other</option>
                </select>
              </Field>
              {isEdit && (
                <Field label="状态">
                  <select
                    value={enabled ? '1' : '0'}
                    onChange={(e) => setEnabled(e.target.value === '1')}
                    className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                  >
                    <option value="1">启用</option>
                    <option value="0">停用</option>
                  </select>
                </Field>
              )}
            </div>
            <Field label="用途说明">
              <input
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            {!isEdit && (
              <Field label="首版内容（可选，留空稍后在版本中创建）">
                <textarea
                  value={form.first_content}
                  onChange={(e) => setForm((f) => ({ ...f, first_content: e.target.value }))}
                  rows={5}
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
            )}
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

function VersionDrawer({
  prompt,
  onClose,
  onChanged,
}: {
  prompt: AdminPrompt | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [versions, setVersions] = useState<PromptVersionItem[]>([]);
  const [activeVersion, setActiveVersion] = useState(0);
  const [newContent, setNewContent] = useState('');
  const [changeNote, setChangeNote] = useState('');
  const [saving, setSaving] = useState(false);

  const load = (id: number) => {
    promptAdminApi
      .versions(id)
      .then((r) => setVersions(r.versions))
      .catch(() => setVersions([]));
  };

  useEffect(() => {
    if (prompt) {
      load(prompt.id);
      setActiveVersion(prompt.active_version);
      setNewContent('');
      setChangeNote('');
    }
  }, [prompt]);

  if (!prompt) return null;

  const createVersion = async () => {
    if (!newContent.trim()) return;
    setSaving(true);
    try {
      const r = await promptAdminApi.createVersion(prompt.id, { content: newContent, change_note: changeNote });
      setActiveVersion(r.version.version);
      setNewContent('');
      setChangeNote('');
      load(prompt.id);
      onChanged();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const activate = async (version: number) => {
    if (!confirm(`激活 v${version}（回滚）？`)) return;
    try {
      await promptAdminApi.activate(prompt.id, version);
      setActiveVersion(version);
      onChanged();
    } catch (e) {
      alert((e as Error).message);
    }
  };

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
          <div className="flex items-center justify-between border-b border-gray-100 px-6 py-4">
            <div>
              <h3 className="text-base font-medium">{prompt.name}</h3>
              <div className="text-xs text-gray-400">{prompt.key} · 当前激活 v{activeVersion || '—'}</div>
            </div>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>

          <div className="flex-1 space-y-4 overflow-y-auto p-6">
            {/* 新版本 */}
            <div className="rounded-2xl border border-gray-100 p-4">
              <div className="mb-2 text-sm font-medium">发布新版本（自动激活）</div>
              <textarea
                value={newContent}
                onChange={(e) => setNewContent(e.target.value)}
                rows={6}
                placeholder="提示词正文…"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary-100"
              />
              <div className="mt-2 flex gap-2">
                <input
                  value={changeNote}
                  onChange={(e) => setChangeNote(e.target.value)}
                  placeholder="变更说明"
                  className="flex-1 rounded-xl border border-gray-200 px-3 py-2 text-xs outline-none focus:ring-2 focus:ring-primary-100"
                />
                <button
                  onClick={createVersion}
                  disabled={saving || !newContent.trim()}
                  className="rounded-xl bg-primary-500 px-4 py-2 text-xs text-white hover:bg-primary-600 disabled:opacity-50"
                >
                  {saving ? '发布中…' : '发布版本'}
                </button>
              </div>
            </div>

            {/* 历史版本 */}
            {versions.map((v) => (
              <div key={v.id} className="rounded-2xl border border-gray-100 p-4">
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">v{v.version}</span>
                    {activeVersion === v.version ? (
                      <span className="flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] text-emerald-600">
                        <Check className="h-3 w-3" /> 当前激活
                      </span>
                    ) : (
                      <button
                        onClick={() => activate(v.version)}
                        className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-500 hover:bg-gray-200"
                      >
                        激活 / 回滚到此版本
                      </button>
                    )}
                  </div>
                  <div className="text-[10px] text-gray-400">
                    {v.created_by || '—'} · {v.created_at?.slice(0, 16).replace('T', ' ') || ''}
                  </div>
                </div>
                {v.change_note && <div className="mb-1 text-xs text-gray-500">变更：{v.change_note}</div>}
                <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded-xl bg-gray-50 p-3 font-mono text-[11px] text-gray-600">
                  {v.content}
                </pre>
              </div>
            ))}
          </div>
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
