import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { BookOpen, Check, History, Plus, X } from 'lucide-react';
import clsx from 'clsx';
import {
  skillAdminApi,
  type SkillItem,
  type SkillVersionItem,
} from '@his/shared';

export function SkillsPage() {
  const [items, setItems] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; skill?: SkillItem } | null>(null);
  const [versionOf, setVersionOf] = useState<SkillItem | null>(null);
  const [error, setError] = useState('');

  const load = () => {
    setLoading(true);
    skillAdminApi
      .list()
      .then((r) => setItems(r.items))
      .catch((e) => {
        setItems([]);
        setError((e as Error).message);
      })
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const toggle = async (s: SkillItem) => {
    try {
      await skillAdminApi.update(s.id, { enabled: !s.enabled });
      load();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-medium">技能管理（SOP 热更新）</h1>
          <p className="mt-0.5 text-xs text-gray-400">
            模型先看「名称 + 一句话简介」的目录，判断相关后才按 key 加载正文；发布 / 回滚只切指针，下一单生效，不用重启
          </p>
        </div>
        <button
          onClick={() => setEditing({ mode: 'create' })}
          className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
        >
          <Plus className="h-4 w-4" /> 新增技能
        </button>
      </div>

      {error && <div className="rounded-xl bg-red-50 px-4 py-2 text-xs text-red-500">{error}</div>}

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
          <BookOpen className="h-10 w-10 text-gray-300" />
          <div className="text-sm text-gray-400">暂无技能，点右上角新增</div>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
          <table className="w-full min-w-[860px] text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                <th className="px-4 py-3 font-normal">名称 / key</th>
                <th className="px-4 py-3 font-normal">简介（喂给模型的目录层）</th>
                <th className="px-4 py-3 font-normal">工具</th>
                <th className="px-4 py-3 font-normal">生效版本</th>
                <th className="px-4 py-3 font-normal">状态</th>
                <th className="px-4 py-3 text-right font-normal">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s) => (
                <tr key={s.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-700">{s.name}</div>
                    <div className="font-mono text-xs text-gray-400">{s.key}</div>
                    {s.mount_hint && <div className="text-[10px] text-gray-400">{s.mount_hint}</div>}
                  </td>
                  <td className="max-w-[280px] px-4 py-3 text-xs text-gray-500">
                    <div className="line-clamp-2">{s.description || '—'}</div>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">
                    {s.tool_ids.length ? `${s.tool_ids.length} 个` : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <span className={clsx(
                      'rounded-full px-2 py-0.5 text-[10px]',
                      s.active_version ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                    )}>
                      {s.active_version ? `v${s.active_version} / ${s.versions} 版` : `未发布（${s.versions} 版草稿）`}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => toggle(s)}
                      title="点击切换启用 / 停用"
                      className={clsx(
                        'rounded-full px-2 py-0.5 text-[10px]',
                        s.enabled ? 'bg-blue-50 text-blue-600' : 'bg-gray-100 text-gray-400',
                      )}
                    >
                      {s.effective ? '生效中' : s.enabled ? '启用未发布' : '停用'}
                    </button>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <button
                        onClick={() => setVersionOf(s)}
                        className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                      >
                        <History className="h-3 w-3" /> 正文版本
                      </button>
                      <button
                        onClick={() => setEditing({ mode: 'edit', skill: s })}
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

      <SkillFormModal
        editing={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          load();
        }}
      />

      <SkillVersionDrawer
        skill={versionOf}
        onClose={() => setVersionOf(null)}
        onChanged={load}
      />
    </div>
  );
}

function SkillFormModal({
  editing,
  onClose,
  onSaved,
}: {
  editing: { mode: 'create' | 'edit'; skill?: SkillItem } | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form, setForm] = useState({
    key: '', name: '', description: '', tool_ids: '', mount_hint: '', content: '', change_note: '',
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (editing?.mode === 'edit' && editing.skill) {
      const s = editing.skill;
      setForm({
        key: s.key, name: s.name, description: s.description,
        tool_ids: s.tool_ids.join(', '), mount_hint: s.mount_hint, content: '', change_note: '',
      });
    } else {
      setForm({ key: '', name: '', description: '', tool_ids: '', mount_hint: '', content: '', change_note: '' });
    }
    setError('');
  }, [editing]);

  if (!editing) return null;
  const isEdit = editing.mode === 'edit';

  const submit = async () => {
    if (!isEdit && !/^[a-z][a-z0-9_]{1,63}$/.test(form.key.trim())) {
      setError('key 需小写字母开头，只能是 a-z / 0-9 / _，长度 2-64');
      return;
    }
    if (!form.name.trim()) {
      setError('请填写名称');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const tool_ids = form.tool_ids.split(/[,，\s]+/).map((t) => t.trim()).filter(Boolean);
      if (isEdit && editing.skill) {
        await skillAdminApi.update(editing.skill.id, {
          name: form.name,
          description: form.description,
          tool_ids,
          mount_hint: form.mount_hint,
        });
      } else {
        await skillAdminApi.create({
          key: form.key.trim(),
          name: form.name,
          description: form.description,
          tool_ids,
          mount_hint: form.mount_hint,
          content: form.content,
          change_note: form.change_note,
        });
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
          className="max-h-[90dvh] w-full max-w-xl overflow-y-auto rounded-2xl bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-medium">{isEdit ? '编辑技能' : '新增技能'}</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="space-y-3.5">
            <div className="grid grid-cols-2 gap-3">
              <Field label="key（模型按它加载正文）">
                <input
                  value={form.key}
                  disabled={isEdit}
                  onChange={(e) => setForm((f) => ({ ...f, key: e.target.value }))}
                  placeholder="如 inquiry"
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-sm outline-none focus:ring-2 focus:ring-primary-100 disabled:bg-gray-50 disabled:text-gray-400"
                />
              </Field>
              <Field label="名称">
                <input
                  value={form.name}
                  onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                  placeholder="如 症状问诊与分诊"
                  className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                />
              </Field>
            </div>
            <Field label="一句话简介（目录层，越短越好，模型据此判断要不要加载）">
              <input
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                placeholder="如 收集症状信息并提供就诊分诊建议"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <Field label="关联工具（逗号分隔，仅展示用，不直接放权）">
              <input
                value={form.tool_ids}
                onChange={(e) => setForm((f) => ({ ...f, tool_ids: e.target.value }))}
                placeholder="call_inquiry_agent, search_knowledge_docs"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            <Field label="挂载说明（备注）">
              <input
                value={form.mount_hint}
                onChange={(e) => setForm((f) => ({ ...f, mount_hint: e.target.value }))}
                placeholder="如 挂载：问诊子 Agent"
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </Field>
            {!isEdit && (
              <>
                <Field label="首版正文（可选，保存后需手动发布才生效）">
                  <textarea
                    value={form.content}
                    onChange={(e) => setForm((f) => ({ ...f, content: e.target.value }))}
                    rows={8}
                    placeholder="# 技能标题&#10;&#10;## 适用&#10;…&#10;&#10;## 步骤&#10;1. …&#10;&#10;## 边界&#10;- …"
                    className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary-100"
                  />
                </Field>
                <Field label="变更说明">
                  <input
                    value={form.change_note}
                    onChange={(e) => setForm((f) => ({ ...f, change_note: e.target.value }))}
                    placeholder="如 初版"
                    className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
                  />
                </Field>
              </>
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

function SkillVersionDrawer({
  skill,
  onClose,
  onChanged,
}: {
  skill: SkillItem | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [versions, setVersions] = useState<SkillVersionItem[]>([]);
  const [activeVersion, setActiveVersion] = useState(0);
  const [newContent, setNewContent] = useState('');
  const [changeNote, setChangeNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');

  useEffect(() => {
    if (skill) {
      skillAdminApi
        .detail(skill.id)
        .then((r) => {
          setVersions(r.versions);
          setActiveVersion(r.skill.active_version);
        })
        .catch(() => setVersions([]));
      setNewContent('');
      setChangeNote('');
      setMsg('');
    }
  }, [skill]);

  if (!skill) return null;

  const saveVersion = async () => {
    if (!newContent.trim()) return;
    setSaving(true);
    try {
      const r = await skillAdminApi.createVersion(skill.id, { content: newContent, change_note: changeNote });
      setMsg(`已保存 v${r.version}（未生效，点发布才会作用到下一单）`);
      setNewContent('');
      setChangeNote('');
      const d = await skillAdminApi.detail(skill.id);
      setVersions(d.versions);
      onChanged();
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const publish = async (version: number) => {
    try {
      const r = await skillAdminApi.publish(skill.id, version);
      setActiveVersion(r.active_version);
      setMsg(`已发布 v${r.active_version}（下一单生效）`);
      onChanged();
    } catch (e) {
      setMsg((e as Error).message);
    }
  };

  const rollback = async (version: number) => {
    if (!confirm(`把生效指针回滚到 v${version}？正文内容不变，只切版本`)) return;
    try {
      const r = await skillAdminApi.rollback(skill.id, version);
      setActiveVersion(r.active_version);
      setMsg(`已回滚到 v${r.active_version}`);
      onChanged();
    } catch (e) {
      setMsg((e as Error).message);
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
          className="flex h-full w-full max-w-2xl flex-col bg-white shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between border-b border-gray-100 px-6 py-4">
            <div>
              <h3 className="text-base font-medium">{skill.name}</h3>
              <div className="font-mono text-xs text-gray-400">
                {skill.key} · 当前生效 v{activeVersion || '—'} · {versions.length} 个版本
              </div>
            </div>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>

          <div className="flex-1 space-y-4 overflow-y-auto p-6">
            <div className="rounded-2xl border border-gray-100 p-4">
              <div className="mb-2 text-sm font-medium">写一版新正文（保存后不自动生效）</div>
              <textarea
                value={newContent}
                onChange={(e) => setNewContent(e.target.value)}
                rows={10}
                placeholder={'# 技能标题\n\n## 适用\n…\n\n## 步骤\n1. …\n\n## 边界\n- …'}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 font-mono text-xs outline-none focus:ring-2 focus:ring-primary-100"
              />
              <div className="mt-2 flex gap-2">
                <input
                  value={changeNote}
                  onChange={(e) => setChangeNote(e.target.value)}
                  placeholder="变更说明（会记进审计）"
                  className="flex-1 rounded-xl border border-gray-200 px-3 py-2 text-xs outline-none focus:ring-2 focus:ring-primary-100"
                />
                <button
                  onClick={saveVersion}
                  disabled={saving || !newContent.trim()}
                  className="rounded-xl bg-primary-500 px-4 py-2 text-xs text-white hover:bg-primary-600 disabled:opacity-50"
                >
                  {saving ? '保存中…' : '保存版本'}
                </button>
              </div>
              {msg && <div className="mt-2 text-xs text-gray-500">{msg}</div>}
            </div>

            {versions.map((v) => (
              <div key={v.version} className="rounded-2xl border border-gray-100 p-4">
                <div className="mb-2 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium">v{v.version}</span>
                    {activeVersion === v.version ? (
                      <span className="flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] text-emerald-600">
                        <Check className="h-3 w-3" /> 当前生效
                      </span>
                    ) : (
                      <div className="flex gap-1">
                        <button
                          onClick={() => publish(v.version)}
                          className="rounded-full bg-primary-50 px-2 py-0.5 text-[10px] text-primary-600 hover:bg-primary-100"
                        >
                          发布此版本
                        </button>
                        {activeVersion > 0 && (
                          <button
                            onClick={() => rollback(v.version)}
                            className="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] text-gray-500 hover:bg-gray-200"
                          >
                            回滚到此版本
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                  <div className="text-[10px] text-gray-400">
                    {v.created_by || '—'} · {v.created_at?.slice(0, 16).replace('T', ' ') || ''}
                  </div>
                </div>
                {(v.change_note || v.published_at) && (
                  <div className="mb-1 text-xs text-gray-500">
                    {v.change_note && <>变更：{v.change_note}</>}
                    {v.published_at && <span className="ml-2 text-gray-400">发布于 {v.published_at.slice(0, 16).replace('T', ' ')}</span>}
                  </div>
                )}
                <pre className="max-h-56 overflow-y-auto whitespace-pre-wrap rounded-xl bg-gray-50 p-3 font-mono text-[11px] text-gray-600">
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
