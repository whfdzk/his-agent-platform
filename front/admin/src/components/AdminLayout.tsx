import { useEffect, useState } from 'react';
import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { motion } from 'framer-motion';
import {
  Activity,
  BarChart3,
  BookOpen,
  Bot,
  Building2,
  Coins,
  CreditCard,
  FileCode2,
  Headset,
  LogOut,
  Menu,
  MessagesSquare,
  Settings2,
  ShieldAlert,
  Stethoscope,
  Users,
  X,
} from 'lucide-react';
import clsx from 'clsx';

import { ADMIN_TENANT_KEY, adminTenantApi, getStoredTenant, type TenantItem } from '@his/shared';
import { useAdminSession } from '../store/adminSession';

const navGroups: { label: string; items: { to: string; label: string; icon: typeof Bot; end?: boolean }[] }[] = [
  {
    label: '总览',
    items: [
      { to: '/', label: '数据看板', icon: BarChart3, end: true },
      { to: '/tokens', label: '数据统计', icon: Coins },
    ],
  },
  {
    label: 'P0 核心管理',
    items: [
      { to: '/users', label: '用户管理', icon: Users },
      { to: '/clinicians', label: '医护授权', icon: Stethoscope },
      { to: '/handoff', label: '人工接管', icon: Headset },
      { to: '/agents', label: 'Agent 管理', icon: Bot },
      { to: '/prompts', label: 'Prompt 管理', icon: FileCode2 },
      { to: '/models', label: '模型管理', icon: Activity },
      { to: '/knowledge', label: '知识库管理', icon: BookOpen },
      { to: '/conversations', label: '对话管理', icon: MessagesSquare },
      { to: '/system', label: '系统配置', icon: Settings2 },
    ],
  },
  {
    label: '审计与扩展',
    items: [
      { to: '/safety', label: '风险审计', icon: ShieldAlert },
      { to: '/members', label: '会员管理', icon: CreditCard },
    ],
  },
];

export function AdminLayout() {
  const { admin, logout } = useAdminSession();
  const navigate = useNavigate();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [tenants, setTenants] = useState<TenantItem[]>([]);
  const [tenantCode, setTenantCode] = useState(() => getStoredTenant() || 'default');

  // 下拉数据只在布局挂载时取一次：租户清单变动频率极低（新医院入驻才变）
  useEffect(() => {
    let alive = true;
    adminTenantApi
      .list()
      .then((r) => {
        if (alive) setTenants(r.tenants);
      })
      .catch(() => {
        /* 取不到就只显示当前 code，不阻塞其他页面 */
      });
    return () => {
      alive = false;
    };
  }, []);

  const switchTenant = (code: string) => {
    if (code === tenantCode) return;
    localStorage.setItem(ADMIN_TENANT_KEY, code);
    setTenantCode(code);
    // 直接刷新：各页面的数据、缓存、已展开的详情都属于旧租户，局部重渲染容易残留
    window.location.reload();
  };

  // 后端只允许超管跨租户（普通管理员带别的 X-Tenant-Code 会 403，
  // 而 403 会触发前端登出，所以这里干脆不给普通管理员看切换器）
  const canSwitchTenant = admin?.role === 'superadmin';

  const signOut = async () => {
    // 先让服务端撤销 token（logout 内部保证本地态一定被清理），再跳登录页
    await logout();
    navigate('/login', { replace: true });
  };

  const sidebarContent = (
    <>
      <div className="flex h-[72px] items-center gap-3 border-b border-slate-100 px-5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary-50 text-primary-600">
          <Activity className="h-[18px] w-[18px]" />
        </div>
        <div>
          <div className="text-sm font-semibold text-slate-900">HIS Agent 平台</div>
          <div className="mt-0.5 text-[11px] text-slate-400">管理控制台</div>
        </div>
        <button
          onClick={() => setMobileNavOpen(false)}
          className="ml-auto flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100 md:hidden"
          aria-label="关闭导航菜单"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-5">
        {navGroups.map((group) => (
          <div key={group.label}>
            <div className="px-3 pb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
              {group.label}
            </div>
            <div className="space-y-0.5">
              {group.items.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  onClick={() => setMobileNavOpen(false)}
                  className={({ isActive }) =>
                    clsx(
                      'flex min-h-9 items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition-colors',
                      isActive
                        ? 'bg-primary-50 font-semibold text-primary-700'
                        : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900',
                    )
                  }
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  {label}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>

      {canSwitchTenant && (
        <div className="border-t border-slate-100 px-4 pt-4">
          <div className="rounded-lg bg-slate-50 p-2.5">
            <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
              <Building2 className="h-3 w-3" />
              工作租户
            </div>
            <select
              value={tenantCode}
              onChange={(e) => switchTenant(e.target.value)}
              className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700 outline-none focus:border-primary-400"
              title="切换后所有管理接口按该租户过滤"
            >
              {(tenants.length ? tenants : [{ id: 1, code: tenantCode, name: tenantCode, status: 'active' }]).map(
                (t) => (
                  <option key={t.code} value={t.code} disabled={t.status !== 'active'}>
                    {t.name}（{t.code}）{t.status !== 'active' ? ' · 已停用' : ''}
                  </option>
                ),
              )}
            </select>
          </div>
        </div>
      )}

      <div className="border-t border-slate-100 px-4 py-4">
        <div className="flex items-center gap-2.5">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold text-slate-600">
            {(admin?.display_name || admin?.username || '管').slice(0, 1)}
          </div>
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs font-medium text-slate-700">
              {admin?.display_name || admin?.username || '管理员'}
            </div>
            <div className="mt-0.5 flex items-center gap-1 text-[10px] text-slate-400">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" /> 已登录
            </div>
          </div>
          <button
            onClick={() => void signOut()}
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700"
            aria-label="退出登录"
            title="退出登录"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </>
  );

  return (
    <div className="flex h-full min-h-0 bg-slate-50">
      <aside className="hidden w-56 shrink-0 flex-col border-r border-slate-200/80 bg-white md:flex lg:w-60 2xl:w-64">
        {sidebarContent}
      </aside>

      {mobileNavOpen && (
        <>
          <button
            className="fixed inset-0 z-40 bg-slate-950/30 md:hidden"
            onClick={() => setMobileNavOpen(false)}
            aria-label="关闭导航菜单"
          />
          <aside className="fixed inset-y-0 left-0 z-50 flex w-[min(19rem,calc(100vw-2.5rem))] flex-col border-r border-slate-200 bg-white shadow-xl md:hidden">
            {sidebarContent}
          </aside>
        </>
      )}

      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto">
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center border-b border-slate-200/80 bg-white/95 px-4 backdrop-blur md:hidden">
          <button
            onClick={() => setMobileNavOpen(true)}
            className="mr-3 flex h-9 w-9 items-center justify-center rounded-lg text-slate-600 hover:bg-slate-100"
            aria-label="打开导航菜单"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="text-sm font-semibold text-slate-800">HIS Agent 管理平台</div>
        </header>
        <motion.div
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
          className="mx-auto w-full max-w-[1680px] p-4 sm:p-5 lg:p-7 2xl:max-w-[1920px] 2xl:p-8"
        >
          <Outlet />
        </motion.div>
      </main>
    </div>
  );
}
