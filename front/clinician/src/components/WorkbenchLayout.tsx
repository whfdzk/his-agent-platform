import { NavLink, useNavigate } from 'react-router-dom';
import { Activity, BellRing, ClipboardList, Headset, LogOut, RefreshCw, Stethoscope, Users } from 'lucide-react';
import clsx from 'clsx';

import { clinicianApi } from '@his/shared';

import { usePolling } from '../hooks/usePolling';
import { useClinicianSession } from '../store/session';
import { RefreshProvider, useRefreshClock, useReportRefresh } from '../store/refreshClock';
import { InlineError, Tag } from './ui';

const NAV = [
  { to: '/queue', label: '待办确认', icon: ClipboardList },
  { to: '/alerts', label: '急症看板', icon: BellRing },
  { to: '/patients', label: '我的患者', icon: Users },
  { to: '/handoff', label: '人工坐席', icon: Headset },
];

function clock(ts: number | null): string {
  if (!ts) return '—';
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false });
}

export function WorkbenchLayout({ children }: { children: React.ReactNode }) {
  // Provider 必须包在最外层：顶栏自己也要上报 scope 的刷新时间
  return (
    <RefreshProvider>
      <LayoutBody>{children}</LayoutBody>
    </RefreshProvider>
  );
}

function LayoutBody({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const { user, logout } = useClinicianSession();
  const { data: scope, error, loading, updatedAt, refresh } = usePolling(
    () => clinicianApi.scope(),
    60_000,
  );
  useReportRefresh('scope', updatedAt);
  // 顶栏显示"屏幕上数据最晚的那次刷新"，而不是单独 scope 的节拍
  const latest = useRefreshClock();

  return (
    <div className="flex min-h-dvh flex-col bg-canvas">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-3 px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary-50 text-primary-700">
              <Stethoscope className="h-4.5 w-4.5" />
            </div>
            <div>
              <div className="text-sm font-semibold text-slate-900">医护工作台</div>
              <div className="text-[11px] text-slate-400">按科室 / 责任患者授权</div>
            </div>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {scope ? (
              <>
                <Tag tone="blue">{scope.department_names.join('、') || '无科室绑定'}</Tag>
                <Tag tone="slate">责任患者 {scope.patient_count}</Tag>
                {scope.can_decide_pending ? <Tag tone="emerald">可代批</Tag> : <Tag tone="amber">只读</Tag>}
              </>
            ) : (
              <Tag tone="slate">{loading ? '加载范围…' : '—'}</Tag>
            )}
            <button
              type="button"
              onClick={() => void refresh()}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100"
              title={`范围与当前页数据最近一次刷新的时间：${clock(latest)}`}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              最近刷新 {clock(latest)}
            </button>
            <span className="hidden text-xs text-slate-600 sm:inline">{user?.name || '医护账号'}</span>
            <button
              type="button"
              onClick={async () => {
                await logout();
                navigate('/login', { replace: true });
              }}
              className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100"
            >
              <LogOut className="h-3.5 w-3.5" />
              退出
            </button>
          </div>
        </div>

        <nav className="mx-auto flex w-full max-w-6xl gap-1 px-4 pb-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm transition-colors',
                  isActive
                    ? 'bg-primary-50 font-medium text-primary-800'
                    : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700',
                )
              }
            >
              <Icon className="h-4 w-4" />
              {label}
            </NavLink>
          ))}
        </nav>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-4">
        {error && (
          <div className="mb-3">
            <InlineError text={error} />
            <div className="mt-1 flex items-center gap-1.5 text-[11px] text-slate-400">
              <Activity className="h-3.5 w-3.5" />
              没有可见范围时请联系管理员为账号绑定科室或责任患者。
            </div>
          </div>
        )}
        {children}
      </main>
    </div>
  );
}
