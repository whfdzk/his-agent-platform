import { NavLink, Outlet, Navigate, useLocation, useNavigate } from 'react-router-dom';
import clsx from 'clsx';
import { Suspense, type ReactNode } from 'react';

import { useUserSession } from '../store/session';
import { TabBar } from './TabBar';
import { NAV_ITEMS } from './nav';

/** 患者端框架：桌面左侧导航 + 移动端底部 TabBar */
export function AppShell() {
  const { user, logout } = useUserSession();
  const navigate = useNavigate();

  return (
    <div className="flex h-full min-h-0 flex-col bg-canvas md:flex-row">
      {/* 桌面侧栏：宽度随屏幕增大而放宽 */}
      <aside className="hidden w-[220px] shrink-0 flex-col border-r border-slate-200 bg-white px-3 py-5 md:flex lg:w-[240px] lg:px-4 2xl:w-[264px]">
        <div className="mb-8 flex items-center gap-2.5 px-1">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary-700 shadow-sm">
            <svg viewBox="0 0 24 24" className="h-5 w-5 text-white" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 12h4l2-6 4 12 2-6h6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-tight text-slate-900">AI 智能慢病管理</div>
            <div className="text-[10px] text-slate-400">患者端 · Health OS</div>
          </div>
        </div>

        <nav className="flex flex-1 flex-col gap-1">
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                clsx(
                  'flex items-center gap-3 rounded-lg border border-transparent px-3 py-2.5 text-sm transition-colors',
                  isActive
                    ? 'border-primary-100 bg-primary-50 font-medium text-primary-700'
                    : 'text-slate-500 hover:bg-slate-50 hover:text-slate-700',
                )
              }
            >
              <Icon className="h-[18px] w-[18px]" />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-4 rounded-xl border border-primary-800/20 bg-primary-800 p-3.5 text-white">
          <div className="text-xs font-medium">需要专业帮助？</div>
          <p className="mt-1 text-[11px] leading-relaxed text-primary-100">
            AI 问诊 7×24 小时在线，急症请直接拨打 120。
          </p>
          <button
            onClick={() => navigate('/consult')}
            className="mt-2 w-full rounded-md bg-white/10 py-1.5 text-[11px] font-medium hover:bg-white/20"
          >
            开始咨询
          </button>
        </div>

        <div className="mt-4 flex items-center gap-2 border-t border-slate-200 pt-4">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-700 text-xs font-medium text-white">
            {(user?.name || '游').slice(0, 1)}
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs font-medium text-slate-700">{user?.name || '未登录'}</div>
            <div className="truncate text-[10px] text-slate-400">{user?.phone || '登录后同步健康数据'}</div>
          </div>
          {user && (
            <button onClick={() => void logout()} className="text-[11px] text-slate-400 hover:text-slate-600">
              退出
            </button>
          )}
        </div>
      </aside>

      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {/* 路由级懒加载的加载态：切换页面时只显示轻量占位，避免白屏 */}
        <Suspense
          fallback={<div className="p-6 text-sm text-slate-400">加载中…</div>}
        >
          <Outlet />
        </Suspense>
      </main>

      <TabBar />
    </div>
  );
}

/** 登录守卫：未登录跳转登录页，登录后回到原页面 */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { token, ready } = useUserSession();
  const location = useLocation();

  if (!ready) {
    return <div className="flex h-40 items-center justify-center text-xs text-slate-400">加载中…</div>;
  }
  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}
