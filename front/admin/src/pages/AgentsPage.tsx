import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertCircle,
  Bot,
  Check,
  ChevronRight,
  FileText,
  LoaderCircle,
  Pencil,
  Plus,
  RefreshCw,
  Rocket,
  Search,
  Settings2,
  SlidersHorizontal,
  Trash2,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import {
  agentAdminApi,
  modelConfigApi,
  promptAdminApi,
  type AdminAgent,
  type AdminPrompt,
  type AgentCatalogSkill,
  type AgentCatalogSubAgent,
  type AgentCatalogTool,
  type AgentRuntimeConfig,
  type AgentStatus,
  type AgentUpsert,
  type ModelConfig,
} from '@his/shared';

const STATUS_META: Record<AgentStatus, { label: string; cls: string }> = {
  draft: { label: '草稿', cls: 'bg-amber-50 text-amber-700 ring-amber-200' },
  published: { label: '已发布', cls: 'bg-emerald-50 text-emerald-700 ring-emerald-200' },
  disabled: { label: '已停用', cls: 'bg-gray-100 text-gray-600 ring-gray-200' },
};

type AgentConfig = AgentRuntimeConfig;

const emptyForm: AgentUpsert = {
  code: '',
  name: '',
  agent_type: 'worker',
  description: '',
  avatar: '',
};

const emptyConfig: AgentConfig = {
  prompt_id: null,
  system_prompt: '',
  model_config_id: null,
  model: '',
  temperature: null,
  max_turns: 0,
  tool_ids: [],
  skill_ids: [],
  sub_agent_codes: [],
  degrade_message: '',
  summarization: { enabled: true, trigger_tokens: 4000, trigger_messages: 6, keep_messages: 6 },
};
const inputClass = 'w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-700 outline-none transition focus:border-primary-400 focus:ring-2 focus:ring-primary-100';

function toStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function toNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function readSummarization(value: unknown): AgentConfig['summarization'] {
  const base = emptyConfig.summarization;
  const source = (value && typeof value === 'object' ? value : {}) as Record<string, unknown>;
  const num = (raw: unknown, fallback: number) => (typeof raw === 'number' && Number.isFinite(raw) && raw > 0 ? raw : fallback);
  return {
    enabled: typeof source.enabled === 'boolean' ? source.enabled : base.enabled,
    trigger_tokens: num(source.trigger_tokens, base.trigger_tokens),
    trigger_messages: num(source.trigger_messages, base.trigger_messages),
    keep_messages: num(source.keep_messages, base.keep_messages),
  };
}

function readConfig(config: AdminAgent['config']): AgentConfig {
  const value = config || {};
  return {
    prompt_id: toNumber(value.prompt_id),
    system_prompt: typeof value.system_prompt === 'string' ? value.system_prompt : '',
    model_config_id: toNumber(value.model_config_id),
    model: typeof value.model === 'string' ? value.model : '',
    temperature: toNumber(value.temperature),
    max_turns: toNumber(value.max_turns) ?? 0,
    tool_ids: toStringList(value.tool_ids),
    skill_ids: toStringList(value.skill_ids),
    sub_agent_codes: toStringList(value.sub_agent_codes),
    degrade_message: typeof value.degrade_message === 'string' ? value.degrade_message : '',
    summarization: readSummarization(value.summarization),
  };
}

function formatDate(value: string | null) {
  return value ? value.slice(0, 16).replace('T', ' ') : '—';
}

export function AgentsPage() {
  const [agents, setAgents] = useState<AdminAgent[]>([]);
  const [catalogTools, setCatalogTools] = useState<AgentCatalogTool[]>([]);
  const [catalogSkills, setCatalogSkills] = useState<AgentCatalogSkill[]>([]);
  const [catalogSubAgents, setCatalogSubAgents] = useState<AgentCatalogSubAgent[]>([]);
  const [allPrompts, setAllPrompts] = useState<AdminPrompt[]>([]);
  const [chatModels, setChatModels] = useState<ModelConfig[]>([]);
  const [syncing, setSyncing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [query, setQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [editing, setEditing] = useState<{ mode: 'create' | 'edit'; agent?: AdminAgent } | null>(null);
  const [selectedAgent, setSelectedAgent] = useState<AdminAgent | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [busyAgentId, setBusyAgentId] = useState<number | null>(null);

  const load = async () => {
    setLoading(true);
    setLoadError('');
    try {
      const result = await agentAdminApi.list();
      setAgents(result.agents);
    } catch (error) {
      setLoadError((error as Error).message || 'Agent 列表加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);
  useEffect(() => {
    Promise.all([agentAdminApi.catalog(), promptAdminApi.list(), modelConfigApi.list('chat')])
      .then(([catalog, promptResult, modelResult]) => {
        setCatalogTools(catalog.tools);
        setCatalogSkills(catalog.skills);
        setCatalogSubAgents(catalog.sub_agents);
        setAllPrompts(promptResult.prompts);
        setChatModels(modelResult.model_configs);
      })
      .catch(() => undefined);
  }, []);

  const syncBuiltins = async () => {
    setSyncing(true);
    try {
      const result = await agentAdminApi.syncBuiltins();
      await load();
      window.alert(
        `同步完成：新增 ${result.created.length} 个，覆盖 ${result.updated.length} 个，已存在跳过 ${result.skipped.length} 个。`
        + (result.created.length ? `\n新增：${result.created.join('、')}` : ''),
      );
    } catch (error) {
      window.alert(`同步失败：${(error as Error).message}`);
    } finally {
      setSyncing(false);
    }
  };

  const visibleAgents = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return agents.filter((agent) => {
      const matchesQuery = !needle || [agent.name, agent.code, agent.description]
        .some((value) => value?.toLocaleLowerCase().includes(needle));
      return matchesQuery
        && (typeFilter === 'all' || agent.agent_type === typeFilter)
        && (statusFilter === 'all' || agent.status === statusFilter);
    });
  }, [agents, query, typeFilter, statusFilter]);

  const openDetails = async (agent: AdminAgent) => {
    setSelectedAgent(agent);
    setDetailLoading(true);
    setDetailError('');
    try {
      const result = await agentAdminApi.get(agent.id);
      setSelectedAgent(result.agent);
    } catch (error) {
      setDetailError((error as Error).message || '详情加载失败');
    } finally {
      setDetailLoading(false);
    }
  };

  const act = async (agent: AdminAgent, action: () => Promise<unknown>, message: string) => {
    setBusyAgentId(agent.id);
    try {
      await action();
      await load();
      if (selectedAgent?.id === agent.id) {
        const result = await agentAdminApi.get(agent.id).catch(() => null);
        if (result) setSelectedAgent(result.agent);
        else setSelectedAgent(null);
      }
    } catch (error) {
      window.alert(`${message}失败：${(error as Error).message}`);
    } finally {
      setBusyAgentId(null);
    }
  };

  const publishedCount = agents.filter((agent) => agent.status === 'published').length;
  const draftCount = agents.filter((agent) => agent.status === 'draft').length;

  return (
    <div className="space-y-5">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-lg font-semibold text-gray-800">Agent 管理</h1>
          <p className="mt-1 text-xs text-gray-500">配置即生效：保存后运行时从数据库装配 Agent（仅「已发布」状态生效）</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            onClick={() => void syncBuiltins()}
            disabled={syncing}
            title="把内置 Agent（supervisor/问诊/用药/知识/报告/健康/就医/运营）补齐到数据库"
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md border border-gray-200 bg-white px-3.5 text-sm font-medium text-gray-700 transition hover:bg-gray-50 disabled:opacity-50"
          >
            {syncing ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />} 同步内置 Agent
          </button>
          <button
            onClick={() => setEditing({ mode: 'create' })}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-primary-600 px-3.5 text-sm font-medium text-white transition hover:bg-primary-700 focus:outline-none focus:ring-2 focus:ring-primary-200"
          >
            <Plus className="h-4 w-4" /> 新增 Agent
          </button>
        </div>
      </header>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Stat label="全部 Agent" value={agents.length} icon={<Bot className="h-4 w-4" />} />
        <Stat label="已发布" value={publishedCount} tone="green" />
        <Stat label="草稿待发布" value={draftCount} tone="amber" className="col-span-2 sm:col-span-1" />
      </div>

      <section className="overflow-hidden rounded-lg border border-gray-200 bg-white">
        <div className="flex flex-col gap-3 border-b border-gray-100 p-3 sm:flex-row sm:items-center">
          <label className="relative min-w-0 flex-1 sm:max-w-sm">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索名称、编码或职责"
              className={`${inputClass} pl-9`}
              aria-label="搜索 Agent"
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <SlidersHorizontal className="hidden h-4 w-4 text-gray-400 sm:block" />
            <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)} className={`${inputClass} w-auto min-w-28`} aria-label="按类型筛选">
              <option value="all">所有类型</option>
              <option value="supervisor">Supervisor</option>
              <option value="worker">Worker</option>
            </select>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} className={`${inputClass} w-auto min-w-28`} aria-label="按状态筛选">
              <option value="all">所有状态</option>
              <option value="draft">草稿</option>
              <option value="published">已发布</option>
              <option value="disabled">已停用</option>
            </select>
          </div>
        </div>

        {loading ? (
          <div className="flex items-center justify-center gap-2 px-4 py-16 text-sm text-gray-500"><LoaderCircle className="h-4 w-4 animate-spin" /> 正在加载 Agent…</div>
        ) : loadError ? (
          <div className="flex flex-col items-center gap-3 px-4 py-14 text-center">
            <AlertCircle className="h-6 w-6 text-red-500" />
            <p className="max-w-md text-sm text-gray-600">{loadError}</p>
            <button onClick={() => void load()} className="rounded-md border border-gray-200 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50">重新加载</button>
          </div>
        ) : agents.length === 0 ? (
          <div className="flex flex-col items-center gap-2 px-4 py-16 text-center">
            <Bot className="h-8 w-8 text-gray-300" />
            <p className="text-sm font-medium text-gray-700">还没有 Agent</p>
            <p className="text-xs text-gray-500">创建 Agent 并配置它可使用的工具、提示词和模型。</p>
            <button onClick={() => setEditing({ mode: 'create' })} className="mt-2 text-sm font-medium text-primary-700 hover:text-primary-800">新增 Agent</button>
          </div>
        ) : visibleAgents.length === 0 ? (
          <div className="px-4 py-14 text-center">
            <p className="text-sm text-gray-600">没有符合条件的 Agent</p>
            <button onClick={() => { setQuery(''); setTypeFilter('all'); setStatusFilter('all'); }} className="mt-2 text-xs font-medium text-primary-700">清除筛选条件</button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="bg-gray-50/80 text-xs text-gray-500">
                <tr>
                  <th className="px-4 py-3 font-medium">Agent</th>
                  <th className="px-4 py-3 font-medium">类型</th>
                  <th className="px-4 py-3 font-medium">配置</th>
                  <th className="px-4 py-3 font-medium">状态</th>
                  <th className="px-4 py-3 font-medium">更新时间</th>
                  <th className="px-4 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {visibleAgents.map((agent) => {
                  const config = readConfig(agent.config);
                  const isBusy = busyAgentId === agent.id;
                  return (
                    <tr key={agent.id} className="group hover:bg-gray-50/70">
                      <td className="max-w-sm px-4 py-3.5">
                        <button onClick={() => void openDetails(agent)} className="block max-w-full text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-300">
                          <span className="block truncate font-medium text-gray-800 group-hover:text-primary-700">{agent.name}</span>
                          <span className="mt-0.5 block truncate font-mono text-xs text-gray-400">{agent.code}</span>
                        </button>
                      </td>
                      <td className="px-4 py-3.5 text-gray-600">{agent.agent_type === 'supervisor' ? 'Supervisor' : 'Worker'}</td>
                      <td className="px-4 py-3.5 text-xs text-gray-500">
                        <span>{config.tool_ids.length} 工具</span><span className="mx-1.5 text-gray-300">·</span>
                        <span>{config.sub_agent_codes.length} 子 Agent</span><span className="mx-1.5 text-gray-300">·</span>
                        <span>温度 {config.temperature ?? '默认'}</span>
                        <ConfigSourceBadge source={agent.config_source} className="ml-1.5" />
                      </td>
                      <td className="px-4 py-3.5"><StatusBadge status={agent.status} /></td>
                      <td className="whitespace-nowrap px-4 py-3.5 text-xs text-gray-500">{formatDate(agent.updated_at || agent.created_at)}</td>
                      <td className="px-4 py-3.5">
                        <div className="flex items-center justify-end gap-1">
                          <button onClick={() => void openDetails(agent)} title="查看详情" aria-label={`查看 ${agent.name} 详情`} className="rounded p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-800"><ChevronRight className="h-4 w-4" /></button>
                          <button onClick={() => setEditing({ mode: 'edit', agent })} title="编辑" aria-label={`编辑 ${agent.name}`} className="rounded p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-800"><Pencil className="h-4 w-4" /></button>
                          {agent.status === 'draft' && (
                            <button disabled={isBusy} onClick={() => window.confirm(`发布 Agent「${agent.name}」？`) && void act(agent, () => agentAdminApi.publish(agent.id), '发布')} title="发布" aria-label={`发布 ${agent.name}`} className="rounded p-1.5 text-emerald-700 hover:bg-emerald-50 disabled:opacity-40"><Rocket className="h-4 w-4" /></button>
                          )}
                          {agent.status === 'published' && (
                            <button disabled={isBusy} onClick={() => window.confirm(`停用 Agent「${agent.name}」？`) && void act(agent, () => agentAdminApi.disable(agent.id), '停用')} title="停用" aria-label={`停用 ${agent.name}`} className="rounded p-1.5 text-amber-700 hover:bg-amber-50 disabled:opacity-40"><Settings2 className="h-4 w-4" /></button>
                          )}
                          {agent.status !== 'published' && (
                            <button disabled={isBusy} onClick={() => window.confirm(`删除 Agent「${agent.name}」？此操作不可撤销。`) && void act(agent, () => agentAdminApi.remove(agent.id), '删除')} title="删除" aria-label={`删除 ${agent.name}`} className="rounded p-1.5 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40"><Trash2 className="h-4 w-4" /></button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="border-t border-gray-100 px-4 py-2.5 text-xs text-gray-400">显示 {visibleAgents.length} / {agents.length} 个 Agent</div>
          </div>
        )}
      </section>

      <AgentFormModal editing={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); void load(); }} />
      <AgentDetailsDrawer
        agent={selectedAgent}
        loading={detailLoading}
        error={detailError}
        busy={selectedAgent ? busyAgentId === selectedAgent.id : false}
        onClose={() => setSelectedAgent(null)}
        onEdit={(agent) => { setSelectedAgent(null); setEditing({ mode: 'edit', agent }); }}
        onPublish={(agent) => void act(agent, () => agentAdminApi.publish(agent.id), '发布')}
        onDisable={(agent) => void act(agent, () => agentAdminApi.disable(agent.id), '停用')}
        tools={catalogTools}
        skills={catalogSkills}
        subAgents={catalogSubAgents}
        prompts={allPrompts}
        models={chatModels}
      />
    </div>
  );
}

function Stat({ label, value, icon, tone = 'blue', className }: { label: string; value: number; icon?: React.ReactNode; tone?: 'blue' | 'green' | 'amber'; className?: string }) {
  const tones = { blue: 'text-blue-700 bg-blue-50', green: 'text-emerald-700 bg-emerald-50', amber: 'text-amber-700 bg-amber-50' };
  return (
    <div className={clsx('flex items-center gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3', className)}>
      <span className={clsx('flex h-8 w-8 items-center justify-center rounded-md', tones[tone])}>{icon || <Check className="h-4 w-4" />}</span>
      <div><div className="text-lg font-semibold leading-5 text-gray-800">{value}</div><div className="mt-1 text-xs text-gray-500">{label}</div></div>
    </div>
  );
}

function StatusBadge({ status }: { status: AgentStatus }) {
  return <span className={clsx('inline-flex items-center rounded-full px-2 py-1 text-[11px] font-medium ring-1 ring-inset', STATUS_META[status].cls)}>{STATUS_META[status].label}</span>;
}

function ConfigSourceBadge({ source, className }: { source: AdminAgent['config_source']; className?: string }) {
  return (
    <span className={clsx(
      'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium',
      source === 'database' ? 'bg-emerald-50 text-emerald-700' : 'bg-gray-100 text-gray-500',
      className,
    )}>
      {source === 'database' ? '库配置生效' : '内置默认'}
    </span>
  );
}

function AgentFormModal({ editing, onClose, onSaved }: { editing: { mode: 'create' | 'edit'; agent?: AdminAgent } | null; onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState<AgentUpsert>(emptyForm);
  const [config, setConfig] = useState<AgentConfig>(emptyConfig);
  const [tools, setTools] = useState<AgentCatalogTool[]>([]);
  const [skills, setSkills] = useState<AgentCatalogSkill[]>([]);
  const [subAgents, setSubAgents] = useState<AgentCatalogSubAgent[]>([]);
  const [prompts, setPrompts] = useState<AdminPrompt[]>([]);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [loadingCatalog, setLoadingCatalog] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const isEdit = editing?.mode === 'edit';

  useEffect(() => {
    if (!editing) return;
    const agent = editing.agent;
    setForm(agent ? {
      code: agent.code, name: agent.name, agent_type: agent.agent_type,
      description: agent.description || '', avatar: agent.avatar || '',
    } : emptyForm);
    setConfig(agent ? readConfig(agent.config) : emptyConfig);
    setError('');
    setLoadingCatalog(true);
    Promise.all([agentAdminApi.catalog(), promptAdminApi.list(), modelConfigApi.list('chat')])
      .then(([catalog, promptResult, modelResult]) => {
        setTools(catalog.tools);
        setSkills(catalog.skills);
        setSubAgents(catalog.sub_agents);
        setPrompts(promptResult.prompts);
        setModels(modelResult.model_configs);
      })
      .catch((loadError) => setError(`配置选项加载失败：${(loadError as Error).message}`))
      .finally(() => setLoadingCatalog(false));
  }, [editing]);

  if (!editing) return null;

  const toggleItem = (key: 'tool_ids' | 'skill_ids' | 'sub_agent_codes', id: string) => {
    setConfig((current) => ({ ...current, [key]: current[key].includes(id) ? current[key].filter((item) => item !== id) : [...current[key], id] }));
  };

  const submit = async () => {
    if (!form.code.trim() || !form.name.trim()) { setError('请填写必填项：编码和名称'); return; }
    if (!/^[a-z][a-z0-9_-]*$/.test(form.code.trim())) { setError('编码仅允许小写字母、数字、中划线和下划线，且以字母开头'); return; }
    if (config.temperature !== null && (config.temperature < 0 || config.temperature > 2)) { setError('温度取值范围为 0 ~ 2'); return; }
    setSaving(true);
    setError('');
    const body: AgentUpsert = {
      ...form,
      code: form.code.trim(),
      name: form.name.trim(),
      config: {
        // 保留后端可能新增的未知字段，只覆盖页面管理的这些项
        ...(editing.agent?.config || {}),
        ...config,
      },
    };
    try {
      if (isEdit && editing.agent) await agentAdminApi.update(editing.agent.id, body);
      else await agentAdminApi.create(body);
      onSaved();
    } catch (saveError) {
      setError((saveError as Error).message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-gray-950/40 p-3 sm:p-6" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div role="dialog" aria-modal="true" aria-labelledby="agent-form-title" className="flex max-h-[94dvh] w-full max-w-3xl flex-col overflow-hidden rounded-lg bg-white shadow-2xl">
        <div className="flex items-center justify-between border-b border-gray-100 px-5 py-4 sm:px-6">
          <div><h2 id="agent-form-title" className="text-base font-semibold text-gray-800">{isEdit ? '编辑 Agent' : '新增 Agent'}</h2><p className="mt-1 text-xs text-gray-500">基本信息与能力配置</p></div>
          <button onClick={onClose} aria-label="关闭" className="rounded p-1.5 text-gray-500 hover:bg-gray-100"><X className="h-4 w-4" /></button>
        </div>
        <div className="overflow-y-auto px-5 py-5 sm:px-6">
          <section>
            <SectionHeading title="基本信息" detail="Agent 身份与职责" />
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="编码" required hint="唯一标识，创建后不可修改">
                <input value={form.code} disabled={isEdit} onChange={(event) => setForm((value) => ({ ...value, code: event.target.value }))} placeholder="例如 inquiry_agent" className={`${inputClass} disabled:bg-gray-50 disabled:text-gray-400`} />
              </Field>
              <Field label="名称" required><input value={form.name} onChange={(event) => setForm((value) => ({ ...value, name: event.target.value }))} placeholder="例如 智慧问诊 Agent" className={inputClass} /></Field>
              <Field label="类型">
                <select value={form.agent_type} onChange={(event) => setForm((value) => ({ ...value, agent_type: event.target.value as AgentUpsert['agent_type'] }))} className={inputClass}>
                  <option value="worker">Worker · 业务执行</option><option value="supervisor">Supervisor · 任务调度</option>
                </select>
              </Field>
              <Field label="职责描述" hint="最多 500 字">
                <textarea maxLength={500} rows={2} value={form.description} onChange={(event) => setForm((value) => ({ ...value, description: event.target.value }))} placeholder="简要说明 Agent 的职责范围" className={`${inputClass} resize-y`} />
              </Field>
            </div>
          </section>

          <section className="mt-7 border-t border-gray-100 pt-5">
            <SectionHeading title="能力与运行配置" detail="选择可用工具、技能、提示词和对话模型" />
            {loadingCatalog ? <div className="flex items-center gap-2 py-8 text-sm text-gray-500"><LoaderCircle className="h-4 w-4 animate-spin" /> 正在加载配置目录…</div> : (
              <div className="space-y-5">
                <div>
                  <div className="mb-2 flex items-center justify-between"><h3 className="text-sm font-medium text-gray-700">工具</h3><span className="text-xs text-gray-400">已选 {config.tool_ids.length}</span></div>
                  {tools.length ? <div className="grid gap-2 sm:grid-cols-2">
                    {tools.map((tool) => <CheckOption key={tool.id} checked={config.tool_ids.includes(tool.id)} title={tool.name} subtitle={`${tool.category} · ${tool.description}`} onChange={() => toggleItem('tool_ids', tool.id)} />)}
                  </div> : <p className="rounded-md bg-gray-50 px-3 py-4 text-xs text-gray-500">暂无可选工具</p>}
                </div>
                <div>
                  <div className="mb-2 flex items-center justify-between"><h3 className="text-sm font-medium text-gray-700">技能</h3><span className="text-xs text-gray-400">已选 {config.skill_ids.length}</span></div>
                  {skills.length ? <div className="grid gap-2 sm:grid-cols-2">
                    {skills.map((skill) => <CheckOption key={skill.id} checked={config.skill_ids.includes(skill.id)} title={skill.name} subtitle={skill.description} onChange={() => toggleItem('skill_ids', skill.id)} />)}
                  </div> : <p className="rounded-md bg-gray-50 px-3 py-4 text-xs text-gray-500">暂无可选技能</p>}
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="提示词" hint={<Link to="/prompts" onClick={onClose} className="font-medium text-primary-700 hover:underline">管理提示词</Link>}>
                    <select value={config.prompt_id ?? ''} onChange={(event) => setConfig((value) => ({ ...value, prompt_id: event.target.value ? Number(event.target.value) : null }))} className={inputClass}>
                      <option value="">不关联提示词</option>
                      {prompts.filter((prompt) => prompt.enabled).map((prompt) => <option key={prompt.id} value={prompt.id}>{prompt.name} · {prompt.key}{prompt.active_version ? ` · v${prompt.active_version}` : ' · 未激活'}</option>)}
                    </select>
                    {prompts.filter((prompt) => prompt.enabled).length === 0 && <p className="mt-1 text-xs text-gray-400">暂无启用的提示词，可前往提示词管理创建。</p>}
                  </Field>
                  <Field label="对话模型" hint={<Link to="/models" onClick={onClose} className="font-medium text-primary-700 hover:underline">模型设置</Link>}>
                    <select value={config.model_config_id ?? ''} onChange={(event) => setConfig((value) => ({ ...value, model_config_id: event.target.value ? Number(event.target.value) : null }))} className={inputClass}>
                      <option value="">使用系统默认模型</option>
                      {models.filter((model) => model.enabled).map((model) => <option key={model.id} value={model.id}>{model.name} · {model.model_name}{model.is_default ? ' · 默认' : ''}</option>)}
                    </select>
                    {models.filter((model) => model.enabled).length === 0 && <p className="mt-1 text-xs text-gray-400">暂无启用的对话模型，可前往模型设置添加。</p>}
                  </Field>
                  <Field label="模型名覆盖" hint="留空则用上方的模型配置">
                    <input value={config.model} onChange={(event) => setConfig((value) => ({ ...value, model: event.target.value }))} placeholder="例如 deepseek-chat" className={inputClass} />
                  </Field>
                  <Field label="温度" hint="0~2，留空用代码内置默认">
                    <input
                      type="number" min={0} max={2} step={0.1} value={config.temperature ?? ''}
                      onChange={(event) => setConfig((value) => ({ ...value, temperature: event.target.value === '' ? null : Number(event.target.value) }))}
                      placeholder="默认" className={inputClass}
                    />
                  </Field>
                  <Field label="最大执行轮数" hint="0 = 框架默认（按 2 倍换算 recursion_limit）">
                    <input
                      type="number" min={0} max={100} value={config.max_turns}
                      onChange={(event) => setConfig((value) => ({ ...value, max_turns: Number(event.target.value) || 0 }))}
                      className={inputClass}
                    />
                  </Field>
                  <Field label="降级话术" hint="worker 调用失败时返回给用户的话术">
                    <input
                      maxLength={500} value={config.degrade_message}
                      onChange={(event) => setConfig((value) => ({ ...value, degrade_message: event.target.value }))}
                      placeholder="可选，留空用内置文案" className={inputClass}
                    />
                  </Field>
                </div>

                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <h3 className="text-sm font-medium text-gray-700">系统提示词</h3>
                    <span className="text-xs text-gray-400">关联提示词且已激活时，以提示词正文为准</span>
                  </div>
                  <textarea
                    rows={6} value={config.system_prompt}
                    onChange={(event) => setConfig((value) => ({ ...value, system_prompt: event.target.value }))}
                    placeholder="Agent 的角色设定与工作规则"
                    className={`${inputClass} resize-y font-mono text-xs leading-5`}
                  />
                </div>

                {form.agent_type === 'supervisor' && (
                  <>
                    <div>
                      <div className="mb-2 flex items-center justify-between"><h3 className="text-sm font-medium text-gray-700">可调度子 Agent</h3><span className="text-xs text-gray-400">已选 {config.sub_agent_codes.length}</span></div>
                      {subAgents.length ? <div className="grid gap-2 sm:grid-cols-2">
                        {subAgents.map((item) => <CheckOption key={item.code} checked={config.sub_agent_codes.includes(item.code)} title={item.name} subtitle={item.code} onChange={() => toggleItem('sub_agent_codes', item.code)} />)}
                      </div> : <p className="rounded-md bg-gray-50 px-3 py-4 text-xs text-gray-500">暂无可用子 Agent</p>}
                    </div>
                    <div className="rounded-md border border-gray-200 p-4">
                      <label className="flex items-center justify-between gap-3">
                        <span>
                          <span className="block text-sm font-medium text-gray-700">会话总结压缩</span>
                          <span className="mt-1 block text-[11px] text-gray-500">对话变长后自动摘要，保留最近若干条消息</span>
                        </span>
                        <input
                          type="checkbox" checked={config.summarization.enabled}
                          onChange={(event) => setConfig((value) => ({ ...value, summarization: { ...value.summarization, enabled: event.target.checked } }))}
                          className="h-4 w-4 shrink-0 accent-primary-600"
                        />
                      </label>
                      <div className="mt-3 grid gap-3 sm:grid-cols-3">
                        <Field label="触发 token 数">
                          <input type="number" min={200} step={100} disabled={!config.summarization.enabled} value={config.summarization.trigger_tokens}
                            onChange={(event) => setConfig((value) => ({ ...value, summarization: { ...value.summarization, trigger_tokens: Number(event.target.value) || 0 } }))}
                            className={`${inputClass} disabled:bg-gray-50 disabled:text-gray-400`} />
                        </Field>
                        <Field label="触发消息数">
                          <input type="number" min={2} disabled={!config.summarization.enabled} value={config.summarization.trigger_messages}
                            onChange={(event) => setConfig((value) => ({ ...value, summarization: { ...value.summarization, trigger_messages: Number(event.target.value) || 0 } }))}
                            className={`${inputClass} disabled:bg-gray-50 disabled:text-gray-400`} />
                        </Field>
                        <Field label="摘要后保留条数">
                          <input type="number" min={1} disabled={!config.summarization.enabled} value={config.summarization.keep_messages}
                            onChange={(event) => setConfig((value) => ({ ...value, summarization: { ...value.summarization, keep_messages: Number(event.target.value) || 0 } }))}
                            className={`${inputClass} disabled:bg-gray-50 disabled:text-gray-400`} />
                        </Field>
                      </div>
                    </div>
                  </>
                )}
              </div>
            )}
          </section>
          <div className="mt-5 flex gap-2 rounded-md border border-blue-200 bg-blue-50 px-3 py-2.5 text-xs leading-5 text-blue-800">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /> 保存即写入数据库并热更新运行时；只有状态为「已发布」的 Agent 使用这里的配置，草稿/停用状态回退代码内置默认。
          </div>
          {error && <div role="alert" className="mt-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
        </div>
        <div className="flex flex-col-reverse gap-2 border-t border-gray-100 bg-gray-50/70 px-5 py-3 sm:flex-row sm:justify-end sm:px-6">
          <button onClick={onClose} className="h-9 rounded-md border border-gray-200 bg-white px-4 text-sm text-gray-700 hover:bg-gray-50">取消</button>
          <button onClick={() => void submit()} disabled={saving || loadingCatalog} className="inline-flex h-9 items-center justify-center gap-2 rounded-md bg-primary-600 px-4 text-sm font-medium text-white hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50">
            {saving && <LoaderCircle className="h-4 w-4 animate-spin" />}{saving ? '保存中…' : '保存 Agent'}
          </button>
        </div>
      </div>
    </div>
  );
}

function AgentDetailsDrawer({ agent, loading, error, busy, onClose, onEdit, onPublish, onDisable, tools, skills, subAgents, prompts, models }: {
  agent: AdminAgent | null; loading: boolean; error: string; busy: boolean; onClose: () => void;
  onEdit: (agent: AdminAgent) => void; onPublish: (agent: AdminAgent) => void; onDisable: (agent: AdminAgent) => void;
  tools: AgentCatalogTool[]; skills: AgentCatalogSkill[]; subAgents: AgentCatalogSubAgent[];
  prompts: AdminPrompt[]; models: ModelConfig[];
}) {
  if (!agent) return null;
  const config = readConfig(agent.config);
  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-gray-950/30" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside role="dialog" aria-modal="true" aria-labelledby="agent-detail-title" className="flex h-full w-full max-w-xl flex-col bg-white shadow-2xl">
        <div className="flex items-start justify-between border-b border-gray-100 px-5 py-4 sm:px-6">
          <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h2 id="agent-detail-title" className="truncate text-base font-semibold text-gray-800">{agent.name}</h2><StatusBadge status={agent.status} /></div><p className="mt-1 font-mono text-xs text-gray-500">{agent.code}</p></div>
          <button onClick={onClose} aria-label="关闭详情" className="rounded p-1.5 text-gray-500 hover:bg-gray-100"><X className="h-4 w-4" /></button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-5 sm:px-6">
          {loading && <div className="mb-4 flex items-center gap-2 text-xs text-gray-500"><LoaderCircle className="h-3.5 w-3.5 animate-spin" /> 正在刷新详情</div>}
          {error && <div role="alert" className="mb-4 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">{error}，当前显示列表中的信息。</div>}
          <section>
            <SectionHeading title="基本信息" />
            <dl className="grid grid-cols-2 gap-x-4 gap-y-4 rounded-md border border-gray-200 p-4">
              <DetailItem label="类型" value={agent.agent_type === 'supervisor' ? 'Supervisor · 任务调度' : 'Worker · 业务执行'} />
              <DetailItem label="Agent ID" value={`#${agent.id}`} />
              <DetailItem label="创建时间" value={formatDate(agent.created_at)} />
              <DetailItem label="更新时间" value={formatDate(agent.updated_at)} />
              <div className="col-span-2"><DetailItem label="职责描述" value={agent.description || '未填写'} /></div>
            </dl>
          </section>
          <section className="mt-7">
            <SectionHeading title="能力配置" detail="当前 Agent 关联的资源" />
            <div className="space-y-4">
              <ConfigList title="工具" values={config.tool_ids} catalog={tools} empty="未配置工具" />
              <ConfigList title="技能" values={config.skill_ids} catalog={skills} empty="未配置技能" />
              <div className="rounded-md border border-gray-200 p-4">
                <div className="flex items-center gap-2 text-xs font-medium text-gray-500"><FileText className="h-3.5 w-3.5" />提示词</div>
                <p className="mt-2 text-sm text-gray-700">{config.prompt_id ? prompts.find((prompt) => prompt.id === config.prompt_id)?.name || `Prompt #${config.prompt_id}` : '未关联提示词'}</p>
                {config.prompt_id && prompts.find((prompt) => prompt.id === config.prompt_id) && <p className="mt-1 text-xs text-gray-500">{prompts.find((prompt) => prompt.id === config.prompt_id)?.key} · v{prompts.find((prompt) => prompt.id === config.prompt_id)?.active_version || '未激活'}</p>}
                <Link to="/prompts" className="mt-2 inline-block text-xs font-medium text-primary-700 hover:underline">打开提示词管理</Link>
              </div>
              <div className="rounded-md border border-gray-200 p-4">
                <div className="flex items-center gap-2 text-xs font-medium text-gray-500"><Settings2 className="h-3.5 w-3.5" />对话模型</div>
                <p className="mt-2 text-sm text-gray-700">{config.model_config_id ? models.find((model) => model.id === config.model_config_id)?.name || `模型配置 #${config.model_config_id}` : '使用系统默认模型'}</p>
                {config.model_config_id && models.find((model) => model.id === config.model_config_id) && <p className="mt-1 text-xs text-gray-500">{models.find((model) => model.id === config.model_config_id)?.model_name}</p>}
                {config.model && <p className="mt-1 text-xs text-gray-500">模型名覆盖：{config.model}</p>}
              </div>
              {agent.agent_type === 'supervisor' && (
                <ConfigList
                  title="可调度子 Agent"
                  values={config.sub_agent_codes}
                  catalog={subAgents.map((item) => ({ id: item.code, name: item.name, description: '', category: '' }))}
                  empty="未配置子 Agent"
                />
              )}
              <div className="rounded-md border border-gray-200 p-4">
                <div className="text-xs font-medium text-gray-500">运行参数</div>
                <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-3">
                  <DetailItem label="温度" value={config.temperature === null ? '代码内置默认' : String(config.temperature)} />
                  <DetailItem label="最大执行轮数" value={config.max_turns > 0 ? `${config.max_turns} 轮` : '框架默认'} />
                  <DetailItem label="会话压缩" value={config.summarization.enabled ? `token ≥ ${config.summarization.trigger_tokens} 或消息 ≥ ${config.summarization.trigger_messages}，保留 ${config.summarization.keep_messages} 条` : '关闭'} />
                  <DetailItem label="降级话术" value={config.degrade_message || '内置默认'} />
                  <div className="col-span-2">
                    <DetailItem label="系统提示词" value={config.system_prompt ? `${config.system_prompt.slice(0, 80)}${config.system_prompt.length > 80 ? '…' : ''}` : '使用代码内置提示词'} />
                  </div>
                </dl>
              </div>
            </div>
          </section>
          <div className={clsx(
            'mt-5 rounded-md p-3 text-xs leading-5',
            agent.config_source === 'database'
              ? 'border border-emerald-200 bg-emerald-50 text-emerald-800'
              : 'border border-amber-200 bg-amber-50 text-amber-800',
          )}>
            {agent.config_source === 'database'
              ? '当前状态为「已发布」，运行时按库里的配置装配 Agent；修改保存后立即生效（无需重启）。'
              : '当前状态不是「已发布」，运行时回退代码内置默认配置；发布后这里的配置才会生效。'}
          </div>
        </div>
        <div className="flex flex-wrap justify-end gap-2 border-t border-gray-100 px-5 py-3 sm:px-6">
          <button onClick={() => onEdit(agent)} className="inline-flex h-9 items-center gap-2 rounded-md border border-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-50"><Pencil className="h-4 w-4" />编辑</button>
          {agent.status === 'draft' && <button disabled={busy} onClick={() => window.confirm(`发布 Agent「${agent.name}」？`) && onPublish(agent)} className="inline-flex h-9 items-center gap-2 rounded-md bg-emerald-700 px-3 text-sm text-white hover:bg-emerald-800 disabled:opacity-50"><Rocket className="h-4 w-4" />发布</button>}
          {agent.status === 'published' && <button disabled={busy} onClick={() => window.confirm(`停用 Agent「${agent.name}」？`) && onDisable(agent)} className="h-9 rounded-md bg-amber-600 px-3 text-sm text-white hover:bg-amber-700 disabled:opacity-50">停用</button>}
        </div>
      </aside>
    </div>
  );
}

function ConfigList({ title, values, catalog, empty }: { title: string; values: string[]; catalog: Array<AgentCatalogTool | AgentCatalogSkill>; empty: string }) {
  const names = values.map((id) => catalog.find((item) => item.id === id)?.name || id);
  return (
    <div className="rounded-md border border-gray-200 p-4">
      <div className="text-xs font-medium text-gray-500">{title}</div>
      {names.length ? <div className="mt-2 flex flex-wrap gap-1.5">{names.map((name) => <span key={name} className="rounded bg-gray-100 px-2 py-1 text-xs text-gray-700">{name}</span>)}</div> : <p className="mt-2 text-sm text-gray-500">{empty}</p>}
    </div>
  );
}

function DetailItem({ label, value }: { label: string; value: string }) {
  return <div><dt className="text-xs text-gray-500">{label}</dt><dd className="mt-1 break-words text-sm text-gray-700">{value}</dd></div>;
}

function CheckOption({ checked, title, subtitle, onChange }: { checked: boolean; title: string; subtitle: string; onChange: () => void }) {
  return (
    <label className={clsx('flex cursor-pointer items-start gap-2.5 rounded-md border p-3 transition', checked ? 'border-primary-300 bg-primary-50/50' : 'border-gray-200 hover:border-gray-300 hover:bg-gray-50')}>
      <input type="checkbox" checked={checked} onChange={onChange} className="mt-0.5 h-4 w-4 shrink-0 accent-primary-600" />
      <span className="min-w-0"><span className="block text-xs font-medium text-gray-700">{title}</span><span className="mt-1 block text-[11px] leading-4 text-gray-500">{subtitle}</span></span>
    </label>
  );
}

function SectionHeading({ title, detail }: { title: string; detail?: string }) {
  return <div className="mb-3"><h3 className="text-sm font-semibold text-gray-800">{title}</h3>{detail && <p className="mt-1 text-xs text-gray-500">{detail}</p>}</div>;
}

function Field({ label, required, hint, children }: { label: string; required?: boolean; hint?: React.ReactNode; children: React.ReactNode }) {
  return <div className="min-w-0"><div className="mb-1.5 flex min-h-4 items-center justify-between gap-2"><label className="text-xs font-medium text-gray-600">{label}{required && <span className="ml-1 text-red-500">*</span>}</label>{hint && <span className="text-[11px] text-gray-400">{hint}</span>}</div>{children}</div>;
}
