import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { authApi, type AuthUser } from '@his/shared';

/**
 * 医护端会话。
 *
 * token 用独立 key（his_clinician_token）：同一台机器同时开着患者端时，
 * 两端登录态互不顶掉。main.tsx 启动时把 shared 的 tokenReader 切到这个 key，
 * 因此 @his/shared 里所有请求自动带医生凭证。
 *
 * 登出仍然先撤销服务端 token，失败也在 finally 清本地：点了退出就必须退出。
 */
const TOKEN_KEY = 'his_clinician_token';
const USER_KEY = 'his_clinician_user';

export function getClinicianToken(): string | undefined {
  return localStorage.getItem(TOKEN_KEY) || undefined;
}

interface ClinicianSession {
  token: string | null;
  user: AuthUser | null;
  ready: boolean;
  login: (token: string, user: AuthUser) => void;
  logout: () => Promise<void>;
}

const SessionContext = createContext<ClinicianSession>({
  token: null,
  user: null,
  ready: false,
  login: () => {},
  logout: async () => {},
});

function clearLocal(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    /* 隐私模式下 storage 不可用，不影响内存态清理 */
  }
}

export function ClinicianSessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [ready, setReady] = useState(false);
  const userRef = useRef<AuthUser | null>(null);
  userRef.current = user;

  useEffect(() => {
    const savedToken = localStorage.getItem(TOKEN_KEY);
    const savedUser = localStorage.getItem(USER_KEY);
    if (savedToken && savedUser) {
      try {
        const u = JSON.parse(savedUser) as AuthUser;
        setToken(savedToken);
        setUser(u);
        // 乐观放行，401/403 再清态；网络错误不登出
        void authApi
          .me(savedToken)
          .then((resp) => {
            localStorage.setItem(USER_KEY, JSON.stringify(resp.user));
            setUser(resp.user);
          })
          .catch(() => {
            /* 由下面的 unauthorized 监听统一处理 */
          });
      } catch {
        clearLocal();
      }
    }
    setReady(true);
  }, []);

  // shared client 在 401 时只清患者端/管理端 key，医生 token 要在这里跟着清，
  // 否则页面会停在"看起来已登录但所有请求都被拒"的假登录。
  useEffect(() => {
    const onUnauthorized = () => {
      if (!userRef.current) return;
      setToken(null);
      setUser(null);
      clearLocal();
    };
    window.addEventListener('his:unauthorized', onUnauthorized);
    return () => window.removeEventListener('his:unauthorized', onUnauthorized);
  }, []);

  const login = useCallback((t: string, u: AuthUser) => {
    setToken(t);
    setUser(u);
    localStorage.setItem(TOKEN_KEY, t);
    localStorage.setItem(USER_KEY, JSON.stringify(u));
  }, []);

  const logout = useCallback(async () => {
    const current = localStorage.getItem(TOKEN_KEY) || undefined;
    try {
      if (current) await authApi.logout(current);
    } catch {
      /* 后端不可用也要能退出 */
    } finally {
      clearLocal();
      setToken(null);
      setUser(null);
    }
  }, []);

  return (
    <SessionContext.Provider value={{ token, user, ready, login, logout }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useClinicianSession() {
  return useContext(SessionContext);
}
