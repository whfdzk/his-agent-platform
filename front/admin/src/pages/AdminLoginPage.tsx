import { useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Activity, Eye, EyeOff, ShieldCheck } from 'lucide-react';

import { adminAuthApi } from '@his/shared';
import { useAdminSession } from '../store/adminSession';

/**
 * 管理端登录页。
 *
 * 账号由 scripts/create_admin.py 离线创建 —— 管理端不提供注册入口，
 * 否则任何人都能给自己开一个管理员。
 *
 * 已登录时直接回跳（来源页优先），避免重复登录。
 */
export function AdminLoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { login, token, ready } = useAdminSession();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  // 守卫重定向时携带的来源路径（仅接受站内相对路径，防开放重定向）
  const fromState = (location.state as { from?: string } | null)?.from;
  const from = fromState && fromState.startsWith('/') && !fromState.startsWith('//') ? fromState : '/';

  if (ready && token) {
    return <Navigate to={from} replace />;
  }

  const submit = async () => {
    if (!username.trim()) {
      setError('请输入管理员账号');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const resp = await adminAuthApi.login({ username: username.trim(), password });
      login(resp.token, resp.admin);
      navigate(from, { replace: true });
    } catch (e) {
      setError((e as Error).message || '登录失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-slate-50 px-4 py-8 sm:px-6">
      <div className="w-full max-w-[420px] rounded-lg border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-md bg-primary-50 text-primary-700">
            <Activity className="h-5 w-5" />
          </div>
          <div>
            <div className="text-sm font-semibold text-slate-900">HIS Agent 平台</div>
            <div className="mt-0.5 flex items-center gap-1 text-xs text-slate-500">
              <ShieldCheck className="h-3.5 w-3.5 text-primary-600" /> 管理控制台
            </div>
          </div>
        </div>

        <h1 className="text-xl font-semibold text-slate-900">管理员登录</h1>
        <p className="mt-1.5 text-sm leading-6 text-slate-500">使用已授权的管理员账号访问平台控制台。</p>

        <form
          className="mt-7 flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div>
            <label htmlFor="admin-username" className="mb-1.5 block text-xs font-medium text-slate-700">账号</label>
            <input
              id="admin-username"
              className="w-full rounded-md border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-primary-500 focus:ring-2 focus:ring-primary-500/15"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              placeholder="管理员账号"
              required
            />
          </div>

          <div>
            <label htmlFor="admin-password" className="mb-1.5 block text-xs font-medium text-slate-700">密码</label>
            <div className="relative">
              <input
                id="admin-password"
                className="w-full rounded-md border border-slate-200 bg-white px-3 py-2.5 pr-10 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-primary-500 focus:ring-2 focus:ring-primary-500/15"
                type={showPwd ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                placeholder="登录密码"
                required
              />
              <button
                type="button"
                onClick={() => setShowPwd((v) => !v)}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-1 text-slate-400 transition hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/30"
                aria-label={showPwd ? '隐藏密码' : '显示密码'}
              >
                {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>

          {error && (
            <div role="alert" className="rounded-md border border-red-100 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">{error}</div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="mt-1 w-full rounded-md bg-primary-600 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-primary-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {loading ? '登录中…' : '登录'}
          </button>
        </form>

        <p className="mt-6 border-t border-slate-100 pt-4 text-center text-xs leading-relaxed text-slate-400">
          账号由运维离线创建，本页不提供注册
        </p>
      </div>
    </div>
  );
}
