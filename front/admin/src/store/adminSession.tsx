import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';
import { ADMIN_TOKEN_KEY, ADMIN_USER_KEY, ApiError, adminAuthApi, clearAdminSession, type AdminUser } from '@his/shared';

/**
 * 管理端会话上下文。
 *
 * 与患者端（his_token / his_user）使用不同的 localStorage key：
 * 同一个浏览器常常既开着患者端又开着管理端，共用 key 会互相顶掉登录态。
 *
 * 登录态的三道校验：
 * 1. 启动时从 localStorage 恢复，并向 /admin/auth/me 校验 token 是否仍有效；
 * 2. 任意请求收到 401/403 时，@his/shared 会派发 his:unauthorized，这里立刻清态；
 * 3. 清态后 RequireAdmin 会把访问者重定向到 /login（并记录来源路径）。
 *
 * 登出：先请求 /admin/auth/logout 让服务端撤销 token（管理员 token 权限大，
 * 不能只依赖前端丢弃 localStorage），无论成功失败都在 finally 中清本地态。
 */
interface AdminSession {
  token: string | null;
  admin: AdminUser | null;
  /** 首帧还没从 localStorage 恢复完，此时不要据 token 为空就跳转登录页 */
  ready: boolean;
  login: (token: string, admin: AdminUser) => void;
  /** 登出；allDevices=true 时同时让该账号其它设备上的 token 失效 */
  logout: (allDevices?: boolean) => Promise<void>;
}

const AdminSessionContext = createContext<AdminSession>({
  token: null,
  admin: null,
  ready: false,
  login: () => {},
  logout: async () => {},
});

const USER_KEY = ADMIN_USER_KEY;

export function AdminSessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [admin, setAdmin] = useState<AdminUser | null>(null);
  const [ready, setReady] = useState(false);

  /** 只清本地态：token 已经失效时不必再打一次登出接口 */
  const clearLocalAdmin = useCallback(() => {
    clearAdminSession();
    setToken(null);
    setAdmin(null);
  }, []);

  const logout = useCallback(
    async (allDevices = false) => {
      // 从 storage 读 token：让 useCallback 依赖稳定，避免启动校验 effect 反复触发
      const current = localStorage.getItem(ADMIN_TOKEN_KEY) || undefined;
      try {
        if (current) await adminAuthApi.logout(current, allDevices);
      } catch {
        // 后端不可用 / token 已失效：本地登出仍然成功，服务端侧由短 TTL 兜底
      } finally {
        clearLocalAdmin();
      }
    },
    [clearLocalAdmin],
  );

  const login = useCallback((t: string, a: AdminUser) => {
    setToken(t);
    setAdmin(a);
    localStorage.setItem(ADMIN_TOKEN_KEY, t);
    localStorage.setItem(USER_KEY, JSON.stringify(a));
  }, []);

  // 任意接口 401/403：共享层已清 localStorage，这里同步清内存态，触发守卫重定向
  useEffect(() => {
    const onUnauthorized = () => {
      setToken(null);
      setAdmin(null);
    };
    window.addEventListener('his:unauthorized', onUnauthorized);
    return () => window.removeEventListener('his:unauthorized', onUnauthorized);
  }, []);

  // 多标签同步：任一标签登出，其它标签同步清态，避免继续使用已撤销的 token
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== ADMIN_TOKEN_KEY) return;
      if (!e.newValue) {
        setToken(null);
        setAdmin(null);
        return;
      }
      try {
        const raw = localStorage.getItem(USER_KEY);
        setToken(e.newValue);
        setAdmin(raw ? (JSON.parse(raw) as AdminUser) : null);
      } catch {
        /* 忽略损坏的本地数据 */
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  // 启动恢复 + 服务端校验：localStorage 里可能残留已过期/被停用的 token
  useEffect(() => {
    const savedToken = localStorage.getItem(ADMIN_TOKEN_KEY);
    const savedUser = localStorage.getItem(USER_KEY);

    if (!savedToken || !savedUser) {
      setReady(true);
      return;
    }

    let parsed: AdminUser;
    try {
      parsed = JSON.parse(savedUser) as AdminUser;
    } catch {
      clearLocalAdmin();
      setReady(true);
      return;
    }

    // 先乐观放行，避免每个页面都白屏等一次网络往返；校验失败再清态跳登录
    setAdmin(parsed);
    setToken(savedToken);
    setReady(true);

    adminAuthApi
      .me()
      .then((resp) => {
        // 以服务端返回的身份为准（display_name 等可能已被管理员改过）
        setAdmin((prev) => ({ ...(prev as AdminUser), ...resp.admin }));
        localStorage.setItem(USER_KEY, JSON.stringify({ ...parsed, ...resp.admin }));
      })
      .catch((e) => {
        // 网络异常不登出（避免后端短暂不可用时把管理员踢掉）；401/403 才登出。
        // token 已失效时只清本地态，不再回调登出接口。
        if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
          clearLocalAdmin();
        }
      });
  }, [clearLocalAdmin]);

  return (
    <AdminSessionContext.Provider value={{ token, admin, ready, login, logout }}>
      {children}
    </AdminSessionContext.Provider>
  );
}

export function useAdminSession() {
  return useContext(AdminSessionContext);
}
