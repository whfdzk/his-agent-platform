import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Activity, ArrowLeft, Eye, EyeOff, ShieldCheck } from 'lucide-react';
import clsx from 'clsx';
import { authApi } from '@his/shared';

import { useUserSession } from '../store/session';

type Mode = 'login' | 'register';

/**
 * 登录 / 注册页：
 * - 注册调用 /auth/register，患者信息写入 patients 表并直接登录
 * - 登录成功后 user_id 切换为患者档案 ID
 */
export function LoginPage() {
  const navigate = useNavigate();
  const { login } = useUserSession();

  const [mode, setMode] = useState<Mode>('login');
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [gender, setGender] = useState('');
  const [age, setAge] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async () => {
    setError('');
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      setError('请输入正确的手机号');
      return;
    }
    if (password.length < 6) {
      setError('密码至少 6 位');
      return;
    }
    if (mode === 'register' && !name.trim()) {
      setError('请填写姓名');
      return;
    }

    setLoading(true);
    try {
      const resp =
        mode === 'login'
          ? await authApi.login({ phone, password })
          : await authApi.register({
              name: name.trim(),
              phone,
              password,
              gender: gender || null,
              age: age ? Number(age) : null,
            });
      login(resp.token, resp.user);
      navigate('/', { replace: true });
    } catch (e) {
      setError((e as Error).message || '请求失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  const inputCls =
    'w-full rounded-md border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-800 outline-none transition-colors placeholder:text-slate-400 focus:border-primary-600 focus:ring-2 focus:ring-primary-600/15';

  return (
    <div className="flex min-h-dvh flex-col bg-canvas">
      <header className="flex items-center gap-1 px-2 pt-3 sm:px-4">
        <button
          type="button"
          onClick={() => navigate(-1)}
          className="flex h-10 w-10 items-center justify-center rounded-md text-slate-500 transition hover:bg-slate-200/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600/30"
          aria-label="返回"
        >
          <ArrowLeft className="h-5 w-5" />
        </button>
      </header>

      <div className="flex flex-1 items-center justify-center px-4 py-6 sm:px-6 sm:py-10">
        <div className="w-full max-w-[420px] rounded-lg border border-slate-200 bg-white p-5 shadow-sm sm:p-8">
          {/* 品牌区 */}
          <div className="mb-7 flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-md bg-primary-50 text-primary-700">
              <Activity className="h-5 w-5" />
            </div>
            <div>
              <div className="text-sm font-semibold text-slate-900">健康管理</div>
              <div className="mt-0.5 text-xs text-slate-500">患者服务平台</div>
            </div>
          </div>
          <div className="mb-6">
            <h1 className="text-xl font-semibold text-slate-900">
              {mode === 'login' ? '登录' : '注册新账号'}
            </h1>
            <p className="mt-1.5 text-sm leading-6 text-slate-500">
              {mode === 'login' ? '使用手机号登录，查看你的问诊与预约记录' : '注册后将为你建立患者档案，用于问诊与挂号'}
            </p>
          </div>

          {/* 登录 / 注册切换 */}
          <div className="mb-5 grid grid-cols-2 rounded-md bg-slate-100 p-1" role="tablist" aria-label="登录或注册">
            {(['login', 'register'] as Mode[]).map((m) => (
              <button
                key={m}
                type="button"
                role="tab"
                aria-selected={mode === m}
                onClick={() => {
                  setMode(m);
                  setError('');
                }}
                className={clsx(
                  'rounded py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600/30',
                  mode === m ? 'bg-white font-semibold text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700',
                )}
              >
                {m === 'login' ? '登录' : '注册'}
              </button>
            ))}
          </div>

          <form
            className="flex flex-col gap-3.5"
            onSubmit={(e) => {
              e.preventDefault();
              handleSubmit();
            }}
          >
            {mode === 'register' && (
              <div>
                <label htmlFor="patient-name" className="mb-1.5 block text-xs font-medium text-slate-700">姓名</label>
                <input id="patient-name" className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="真实姓名" autoComplete="name" />
              </div>
            )}

            <div>
              <label htmlFor="patient-phone" className="mb-1.5 block text-xs font-medium text-slate-700">手机号</label>
              <input
                id="patient-phone"
                className={inputCls}
                value={phone}
                onChange={(e) => setPhone(e.target.value.replace(/\D/g, '').slice(0, 11))}
                inputMode="numeric"
                autoComplete="tel"
                placeholder="11 位手机号"
              />
            </div>

            <div>
              <label htmlFor="patient-password" className="mb-1.5 block text-xs font-medium text-slate-700">密码</label>
              <div className="relative">
                <input
                  id="patient-password"
                  className={clsx(inputCls, 'pr-10')}
                  type={showPwd ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
                  placeholder="至少 6 位"
                />
                <button
                  type="button"
                  onClick={() => setShowPwd((v) => !v)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-1 text-slate-400 transition hover:text-slate-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600/30"
                  aria-label={showPwd ? '隐藏密码' : '显示密码'}
                >
                  {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            {mode === 'register' && (
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="patient-gender" className="mb-1.5 block text-xs font-medium text-slate-700">性别</label>
                  <select id="patient-gender" className={inputCls} value={gender} onChange={(e) => setGender(e.target.value)}>
                    <option value="">不填</option>
                    <option value="男">男</option>
                    <option value="女">女</option>
                  </select>
                </div>
                <div>
                  <label htmlFor="patient-age" className="mb-1.5 block text-xs font-medium text-slate-700">年龄</label>
                  <input
                    id="patient-age"
                    className={inputCls}
                    value={age}
                    onChange={(e) => setAge(e.target.value.replace(/\D/g, '').slice(0, 3))}
                    inputMode="numeric"
                    placeholder="选填"
                  />
                </div>
              </div>
            )}

            {error && <div role="alert" className="rounded-md border border-red-100 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">{error}</div>}

            <button
              type="submit"
              disabled={loading}
              className="mt-1 w-full rounded-md bg-primary-700 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-primary-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600/40 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? '提交中…' : mode === 'login' ? '登录' : '注册并登录'}
            </button>
          </form>

          <p className="mt-6 flex items-start justify-center gap-1.5 border-t border-slate-100 pt-4 text-center text-xs leading-relaxed text-slate-400">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>注册即表示同意将问诊信息用于分诊与挂号服务</span>
          </p>
        </div>
      </div>
    </div>
  );
}
