import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Eye, EyeOff, ShieldCheck, Stethoscope } from 'lucide-react';
import clsx from 'clsx';

import { authApi, clinicianApi } from '@his/shared';

import { useClinicianSession } from '../store/session';

/**
 * 医护登录：复用患者账号体系（patients 表 + rbac_subject_roles 的 clinician/nurse 角色）。
 * 登录成功后必须先探一次 /clinician/scope —— 有 token 不等于有医护权限，
 * 普通患者账号能登录患者端，但绝不该进到工作台看到别人的患者。
 */
export function LoginPage() {
  const navigate = useNavigate();
  const { login } = useClinicianSession();

  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    setError('');
    if (!/^1[3-9]\d{9}$/.test(phone)) {
      setError('请输入正确的手机号');
      return;
    }
    if (password.length < 6) {
      setError('密码至少 6 位');
      return;
    }

    setLoading(true);
    try {
      const resp = await authApi.login({ phone, password });
      login(resp.token, resp.user);
      try {
        await clinicianApi.scope();
      } catch (e) {
        setError((e as Error).message || '该账号没有医护权限');
        return;
      }
      navigate('/queue', { replace: true });
    } catch (e) {
      setError((e as Error).message || '登录失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-canvas px-4 py-10">
      <div className="w-full max-w-[400px] rounded-xl border border-slate-200 bg-white p-6 shadow-card sm:p-8">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary-50 text-primary-700">
            <Stethoscope className="h-5 w-5" />
          </div>
          <div>
            <div className="text-sm font-semibold text-slate-900">医护工作台</div>
            <div className="mt-0.5 text-[11px] text-slate-400">仅限已授予医生 / 护士角色的账号</div>
          </div>
        </div>

        <h1 className="text-xl font-semibold text-slate-900">登录</h1>
        <p className="mt-1 text-sm text-slate-500">使用院内账号登录，查看你责任范围与本科室告警</p>

        <form
          className="mt-5 flex flex-col gap-3.5"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <div>
            <label htmlFor="clinician-phone" className="label-base">
              手机号
            </label>
            <input
              id="clinician-phone"
              className="input-base"
              value={phone}
              onChange={(e) => setPhone(e.target.value.replace(/\D/g, '').slice(0, 11))}
              inputMode="numeric"
              autoComplete="tel"
              placeholder="11 位手机号"
            />
          </div>

          <div>
            <label htmlFor="clinician-password" className="label-base">
              密码
            </label>
            <div className="relative">
              <input
                id="clinician-password"
                className={clsx('input-base pr-10')}
                type={showPwd ? 'text' : 'password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                placeholder="至少 6 位"
              />
              <button
                type="button"
                onClick={() => setShowPwd((v) => !v)}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-1 text-slate-400 hover:text-slate-700"
                aria-label={showPwd ? '隐藏密码' : '显示密码'}
              >
                {showPwd ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </div>
          </div>

          {error && (
            <div role="alert" className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={loading}
            className="mt-1 w-full rounded-lg bg-primary-700 py-2.5 text-sm font-semibold text-white transition-colors hover:bg-primary-800 disabled:opacity-50"
          >
            {loading ? '登录中…' : '登录'}
          </button>
        </form>

        <p className="mt-5 flex items-start gap-1.5 border-t border-slate-100 pt-4 text-[11px] leading-relaxed text-slate-400">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>可见范围由科室绑定与责任患者决定，无绑定的账号会被直接拒绝而不是看到空列表。</span>
        </p>
      </div>
    </div>
  );
}
