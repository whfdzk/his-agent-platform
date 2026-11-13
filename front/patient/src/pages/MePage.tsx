import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import {
  CalendarDays,
  ChevronRight,
  ClipboardList,
  Download,
  FileText,
  LogOut,
  ShieldCheck,
  Trash2,
  UserRound,
} from 'lucide-react';
import { patientApi } from '@his/shared';
import type { ConsentItem, DataRequestItem } from '@his/shared';

import { useUserSession } from '../store/session';
import { Button, Card, SectionHeader, Sheet, Tag } from '../components/ui';

const LINKS = [
  { to: '/profile', label: '健康档案', icon: UserRound, hint: '基本资料 / 病史 / 过敏史' },
  { to: '/reports', label: '检查报告', icon: FileText, hint: '上传、解析与指标对比' },
  { to: '/appointments', label: '我的预约', icon: CalendarDays, hint: '挂号与就诊安排' },
  { to: '/history', label: '问诊记录', icon: ClipboardList, hint: '历史 AI 咨询会话' },
];

export function MePage() {
  const { user, token, logout } = useUserSession();
  const navigate = useNavigate();
  const [consents, setConsents] = useState<ConsentItem[]>([]);
  const [requests, setRequests] = useState<DataRequestItem[]>([]);
  const [busy, setBusy] = useState<string>('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [policyOpen, setPolicyOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const [c, r] = await Promise.all([patientApi.consents(), patientApi.dataRequests()]);
      setConsents(c.consents);
      setRequests(r.requests);
    } catch (e) {
      setError((e as Error).message || '加载失败');
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleConsent = async (item: ConsentItem) => {
    setBusy(item.scope);
    setError('');
    try {
      await patientApi.updateConsent(item.scope, !item.granted);
      await load();
    } catch (e) {
      setError((e as Error).message || '更新失败');
    } finally {
      setBusy('');
    }
  };

  const exportData = async () => {
    setBusy('export');
    setError('');
    setMessage('');
    try {
      const res = await patientApi.exportData();
      if (res.download_url) {
        const resp = await fetch(res.download_url, {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        if (!resp.ok) throw new Error('下载失败');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `health-data-${dayjs().format('YYYYMMDD')}.json`;
        a.click();
        URL.revokeObjectURL(url);
      }
      setMessage(res.message);
      await load();
    } catch (e) {
      setError((e as Error).message || '导出失败');
    } finally {
      setBusy('');
    }
  };

  const requestDelete = async () => {
    setBusy('delete');
    setError('');
    try {
      const res = await patientApi.deleteAccount();
      setMessage(res.message);
      setDeleteOpen(false);
      await load();
    } catch (e) {
      setError((e as Error).message || '提交失败');
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-5 md:pb-10 2xl:max-w-5xl">
        <header className="mb-4">
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">个人中心</h1>
          <p className="mt-0.5 text-xs text-slate-400">账号、授权与数据管理</p>
        </header>

        {/* 账号 */}
        <Card className="mb-4 flex items-center gap-3 p-4">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-primary-600 to-primary-800 text-base font-medium text-white">
            {(user?.name || '游').slice(0, 1)}
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold text-slate-900">{user?.name || '未登录'}</div>
            <div className="mt-0.5 text-[11px] text-slate-400">
              {user?.phone || '登录后可用健康档案与报告功能'}
            </div>
          </div>
          {user ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy === 'logout'}
              onClick={async () => {
                // 先让服务端撤销 token，本地清理在 logout 内部 finally 中执行
                setBusy('logout');
                try {
                  await logout();
                } finally {
                  setBusy('');
                  navigate('/');
                }
              }}
            >
              <LogOut className="h-3.5 w-3.5" /> {busy === 'logout' ? '退出中…' : '退出'}
            </Button>
          ) : (
            <Button size="sm" onClick={() => navigate('/login')}>
              登录
            </Button>
          )}
        </Card>

        {message && (
          <div className="mb-3 rounded-xl bg-emerald-50 px-3 py-2 text-xs text-emerald-600">{message}</div>
        )}
        {error && <div className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{error}</div>}

        {/* 快捷入口 */}
        <Card className="mb-5 divide-y divide-slate-100">
          {LINKS.map(({ to, label, icon: Icon, hint }) => (
            <Link key={to} to={to} className="flex items-center gap-3 px-4 py-3.5">
              <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-50 text-slate-500">
                <Icon className="h-4 w-4" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-slate-700">{label}</div>
                <div className="text-[10px] text-slate-400">{hint}</div>
              </div>
              <ChevronRight className="h-4 w-4 text-slate-300" />
            </Link>
          ))}
        </Card>

        {/* 账号安全：服务端撤销能力对用户可见，怀疑被盗号时一键下线所有设备 */}
        {user && (
          <>
            <SectionHeader title="账号安全" subtitle="设备与会话" />
            <Card className="mb-5">
              <div className="flex items-center gap-3 px-4 py-3.5">
                <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-50 text-slate-500">
                  <ShieldCheck className="h-4 w-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-medium text-slate-700">退出全部设备</div>
                  <div className="text-[10px] text-slate-400">
                    其它设备上的登录会立即失效，需要重新登录
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy === 'logout-all'}
                  onClick={async () => {
                    setBusy('logout-all');
                    try {
                      // all_devices=true：服务端记录失效时间点，其它设备 token 一并作废
                      await logout(true);
                    } finally {
                      setBusy('');
                      navigate('/');
                    }
                  }}
                >
                  {busy === 'logout-all' ? '处理中…' : '全部退出'}
                </Button>
              </div>
            </Card>
          </>
        )}

        {/* 授权管理 */}
        <SectionHeader title="授权管理" subtitle="随时可撤回，撤回后立即生效" />
        <Card className="mb-5 divide-y divide-slate-100">
          {consents.map((c) => (
            <div key={c.scope} className="flex items-center gap-3 px-4 py-3.5">
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-slate-700">{c.label}</div>
                <div className="mt-0.5 text-[10px] text-slate-400">
                  {c.granted
                    ? c.granted_at
                      ? `已授权 · ${dayjs(c.granted_at).format('YYYY-MM-DD')}`
                      : '已授权'
                    : '未授权'}
                  {' · '}
                  政策版本 {c.version}
                </div>
              </div>
              <button
                disabled={busy === c.scope}
                onClick={() => toggleConsent(c)}
                className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
                  c.granted ? 'bg-primary-600' : 'bg-slate-200'
                } disabled:opacity-60`}
                aria-label={`切换${c.label}`}
              >
                <span
                  className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
                    c.granted ? 'left-[18px]' : 'left-0.5'
                  }`}
                />
              </button>
            </div>
          ))}
        </Card>

        {/* 数据管理 */}
        <SectionHeader title="我的数据" subtitle="依据个人信息保护要求提供导出与删除" />
        <Card className="mb-5 divide-y divide-slate-100">
          <div className="flex items-center gap-3 px-4 py-3.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-sky-50 text-sky-600">
              <Download className="h-4 w-4" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-slate-700">导出我的健康数据</div>
              <div className="text-[10px] text-slate-400">JSON 格式，包含档案、指标、报告指标与问诊记录</div>
            </div>
            <Button size="sm" variant="secondary" onClick={exportData} disabled={busy === 'export'}>
              {busy === 'export' ? '生成中…' : '导出'}
            </Button>
          </div>
          <div className="flex items-center gap-3 px-4 py-3.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-red-50 text-red-500">
              <Trash2 className="h-4 w-4" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-slate-700">删除账号与健康数据</div>
              <div className="text-[10px] text-slate-400">需人工核验身份，医疗记录依法保留</div>
            </div>
            <Button size="sm" variant="danger" onClick={() => setDeleteOpen(true)}>
              申请
            </Button>
          </div>
          <button
            onClick={() => setPolicyOpen(true)}
            className="flex w-full items-center gap-3 px-4 py-3.5 text-left"
          >
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary-50 text-primary-700">
              <ShieldCheck className="h-4 w-4" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-slate-700">隐私政策与数据使用说明</div>
              <div className="text-[10px] text-slate-400">数据加密存储、访问审计与保留周期</div>
            </div>
            <ChevronRight className="h-4 w-4 text-slate-300" />
          </button>
        </Card>

        {/* 申请记录 */}
        <SectionHeader title="申请记录" subtitle={`${requests.length} 条`} />
        <Card className="divide-y divide-slate-100">
          {requests.length === 0 ? (
            <div className="px-4 py-4 text-center text-xs text-slate-400">暂无导出或删除申请</div>
          ) : (
            requests.map((r) => (
              <div key={r.id} className="flex items-center gap-3 px-4 py-3">
                <Tag tone={r.request_type === 'export' ? 'sky' : 'red'}>
                  {r.request_type === 'export' ? '数据导出' : '删除申请'}
                </Tag>
                <div className="min-w-0 flex-1">
                  <div className="text-[11px] text-slate-600">{r.note || '—'}</div>
                  <div className="text-[10px] text-slate-400">
                    {dayjs(r.created_at || undefined).format('YYYY-MM-DD HH:mm')}
                    {r.expires_at ? ` · ${dayjs(r.expires_at).format('MM-DD')} 前有效` : ''}
                  </div>
                </div>
                <Tag tone={r.status === 'completed' ? 'emerald' : r.status === 'failed' ? 'red' : 'amber'}>
                  {r.status === 'completed'
                    ? '已完成'
                    : r.status === 'pending'
                      ? '待处理'
                      : r.status === 'failed'
                        ? '失败'
                        : r.status}
                </Tag>
              </div>
            ))
          )}
        </Card>

        <p className="mt-6 text-center text-[10px] leading-relaxed text-slate-400">
          访问日志与模型调用记录仅管理员可查，并按保留周期自动清理。
        </p>
      </div>

      <Sheet
        open={deleteOpen}
        title="申请删除账号与数据"
        onClose={() => setDeleteOpen(false)}
        footer={
          <div className="space-y-2">
            <Button block variant="danger" onClick={requestDelete} disabled={busy === 'delete'}>
              {busy === 'delete' ? '提交中…' : '确认提交申请'}
            </Button>
            <Button block variant="ghost" onClick={() => setDeleteOpen(false)}>
              再想想
            </Button>
          </div>
        }
      >
        <div className="space-y-2 text-xs leading-relaxed text-slate-600">
          <p>提交后我们会在 7 个工作日内完成身份核验，并删除可删除的健康数据、撤回全部授权。</p>
          <p className="rounded-xl bg-amber-50 px-3 py-2 text-amber-700">
            依据医疗文书管理要求，问诊记录、处方与检查报告等诊疗数据可能需在法定保留期内留存，不会被立即物理删除。
          </p>
        </div>
      </Sheet>

      <Sheet open={policyOpen} title="隐私政策与数据使用" onClose={() => setPolicyOpen(false)}>
        <div className="space-y-3 text-xs leading-relaxed text-slate-600">
          <p>
            <strong className="text-slate-800">数据范围：</strong>
            健康档案、体征记录、报告结构化指标、问诊会话与授权记录。
          </p>
          <p>
            <strong className="text-slate-800">使用目的：</strong>
            仅用于本人健康管理、AI 分诊建议与报告解读；不用于广告投放或模型训练（除非单独授权研究用途）。
          </p>
          <p>
            <strong className="text-slate-800">安全措施：</strong>
            报告原文存于私有对象存储，数据库只存结构化指标；日志与链路追踪全部脱敏，不记录身份证号、手机号与报告原文。
          </p>
          <p>
            <strong className="text-slate-800">访问与保留：</strong>
            访问日志保留 30 天，模型调用与 Agent 轨迹保留 180 天，安全事件保留 365 天；到期自动清理。
          </p>
          <p>
            <strong className="text-slate-800">你的权利：</strong>
            可随时撤回授权、导出数据或申请删除账号；撤回 AI 咨询授权后将停止新的 AI 处理。
          </p>
        </div>
      </Sheet>
    </div>
  );
}
