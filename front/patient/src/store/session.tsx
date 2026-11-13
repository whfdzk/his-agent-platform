import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { ApiError, authApi, clearPatientSession, type AuthUser } from '@his/shared';

/**
 * 患者端会话上下文：
 * - 登录态：token + user 持久化 localStorage；登录后 user_id 使用患者档案 ID
 * - 未登录：user_id 为本地 uuid
 * - sessionId：每个会话一个，可切换（旧会话任务后台继续）
 *
 * 登出不再是"只清本地"：先请求后端撤销当前 token（服务端 Redis 黑名单），
 * 无论请求成功与否都在 finally 中清理本地态 —— 用户点了退出就必须退出，
 * 不能因为网络问题或后端异常卡在已登录状态。
 */
interface UserSession {
  token: string | null;
  user: AuthUser | null;
  /** 后端 user_id：登录后为患者档案 ID，未登录为本地 uuid */
  userId: string;
  sessionId: string;
  /** localStorage 恢复完成：避免首帧把已登录用户误判为未登录而跳转登录页 */
  ready: boolean;
  login: (token: string, user: AuthUser) => void;
  /** 登出；allDevices=true 时同时让该账号其它设备上的 token 失效 */
  logout: (allDevices?: boolean) => Promise<void>;
  /** 开启新会话 */
  newSession: () => void;
  /** 切回某个会话 */
  switchSession: (sid: string) => void;
}

function uuid(): string {
  return crypto.randomUUID().replace(/-/g, '').slice(0, 16);
}

const UserSessionContext = createContext<UserSession>({
  token: null,
  user: null,
  userId: '',
  sessionId: '',
  ready: false,
  login: () => {},
  logout: async () => {},
  newSession: () => {},
  switchSession: () => {},
});

export function UserSessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [userId, setUserId] = useState('');
  const [sessionId, setSessionId] = useState('');
  const [ready, setReady] = useState(false);

  // 事件回调里需要读最新的 user，用 ref 避免把 user 塞进依赖导致 clearLocal 反复重建
  const userRef = useRef<AuthUser | null>(null);
  userRef.current = user;

  /** 清空内存态 + 本地凭证，并把身份退回匿名 uuid（登出/失效共用） */
  const clearLocal = useCallback(() => {
    setToken(null);
    setUser(null);
    clearPatientSession();

    // 匿名身份恢复为 uuid：登录期间的 his_user_id 是档案 ID，不能继续沿用
    const uid = localStorage.getItem('his_user_id');
    const anonymous = uid && uid === String(userRef.current?.id) ? uuid() : uid || uuid();
    localStorage.setItem('his_user_id', anonymous);
    setUserId(anonymous);

    // 换一个临时会话：登出后继续聊天不应写回登录期间的会话
    const sid = uuid();
    localStorage.setItem('his_session_id', sid);
    setSessionId(sid);

    // 通知其他模块（如 useChatStream）中断进行中的流式请求
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('his:logout'));
    }
  }, []);

  useEffect(() => {
    // 恢复登录态
    const savedToken = localStorage.getItem('his_token');
    const savedUser = localStorage.getItem('his_user');
    if (savedToken && savedUser) {
      try {
        const u = JSON.parse(savedUser) as AuthUser;
        setToken(savedToken);
        setUser(u);
        localStorage.setItem('his_user_id', String(u.id));
        setUserId(String(u.id));

        // 服务端校验：本地可能残留已过期 / 已登出 / 被停用的 token。
        // 先乐观放行避免白屏，401/403 再清态；网络异常不登出。
        void authApi
          .me(savedToken)
          .then((resp) => {
            localStorage.setItem('his_user', JSON.stringify(resp.user));
            localStorage.setItem('his_user_id', String(resp.user.id));
            setUser(resp.user);
            setUserId(String(resp.user.id));
          })
          .catch((e) => {
            if (e instanceof ApiError && (e.status === 401 || e.status === 403)) {
              // 已登出 / 被停用：客户端清理逻辑统一走 clearLocal（含匿名身份回收）
              clearLocal();
            }
          });
      } catch {
        localStorage.removeItem('his_token');
        localStorage.removeItem('his_user');
      }
    }

    if (!localStorage.getItem('his_user_id')) {
      localStorage.setItem('his_user_id', uuid());
    }
    setUserId((prev) => prev || localStorage.getItem('his_user_id') || '');

    let sid = localStorage.getItem('his_session_id');
    if (!sid) {
      sid = uuid();
      localStorage.setItem('his_session_id', sid);
    }
    setSessionId(sid);
    setReady(true);
    // clearLocal 是稳定引用（依赖为空），这里只跑一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = (t: string, u: AuthUser) => {
    setToken(t);
    setUser(u);
    localStorage.setItem('his_token', t);
    localStorage.setItem('his_user', JSON.stringify(u));
    // 登录后 user_id 切换为患者档案 ID，预约/记录与档案关联
    localStorage.setItem('his_user_id', String(u.id));
    setUserId(String(u.id));
  };

  const logout = useCallback(
    async (allDevices = false) => {
      // 从 storage 读而不是依赖闭包：保证 useCallback 依赖稳定，不会因 token 变化重建
      const current = localStorage.getItem('his_token') || undefined;
      try {
        if (current) await authApi.logout(current, allDevices);
      } catch {
        // 后端不可用 / token 已失效：本地登出仍然成功，服务端侧由短 TTL 兜底
      } finally {
        clearLocal();
      }
    },
    [clearLocal],
  );

  // 接口返回 401 时（token 过期/被注销）：同步清空内存登录态，
  // 否则 React 仍认为已登录，页面只会显示空数据而不会跳转登录页
  useEffect(() => {
    const onUnauthorized = () => {
      const wasLoggedIn = !!userRef.current;
      setToken(null);
      setUser(null);
      if (wasLoggedIn) {
        // 身份退回匿名 uuid，避免匿名态继续复用患者档案 ID 的本地数据
        const anonymous = uuid();
        localStorage.setItem('his_user_id', anonymous);
        setUserId(anonymous);
      }
    };
    window.addEventListener('his:unauthorized', onUnauthorized);
    return () => window.removeEventListener('his:unauthorized', onUnauthorized);
  }, []);

  // 多标签同步：任一标签登出/登录，其它标签跟随，避免"一个标签退了、另一个还在用旧 token"
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== 'his_token') return;
      if (!e.newValue) {
        // 其它标签已登出：身份同样退回匿名 uuid，避免匿名态复用患者档案 ID
        const wasLoggedIn = !!userRef.current;
        setToken(null);
        setUser(null);
        if (wasLoggedIn) {
          const anonymous = uuid();
          localStorage.setItem('his_user_id', anonymous);
          setUserId(anonymous);
        }
        return;
      }
      try {
        const raw = localStorage.getItem('his_user');
        setToken(e.newValue);
        setUser(raw ? (JSON.parse(raw) as AuthUser) : null);
        const uid = localStorage.getItem('his_user_id');
        if (uid) setUserId(uid);
      } catch {
        /* 忽略损坏的本地数据 */
      }
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  const newSession = () => {
    // 未登录（游客）不允许新增会话：只能使用当前临时会话聊天，且不会有历史记录
    if (!token) return;
    const sid = uuid();
    localStorage.setItem('his_session_id', sid);
    setSessionId(sid);
  };

  const switchSession = (sid: string) => {
    localStorage.setItem('his_session_id', sid);
    setSessionId(sid);
  };

  return (
    <UserSessionContext.Provider
      value={{ token, user, userId, sessionId, ready, login, logout, newSession, switchSession }}
    >
      {children}
    </UserSessionContext.Provider>
  );
}

export function useUserSession() {
  return useContext(UserSessionContext);
}
