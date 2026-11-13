import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import { LogOut, Plus, RefreshCw } from 'lucide-react';
import clsx from 'clsx';
import { sessionApi, type SessionSummary } from '@his/shared';

import { useUserSession } from '../store/session';
import { listLocalSessions } from '../lib/sessionRegistry';

interface SidebarItem {
  session_id: string;
  title: string;
  time: string | null;
  is_inquiry_active: boolean;
}

/**
 * 左侧会话列表侧栏：
 * - 登录后显示历史会话：后端 /sessions 结果 + 本地登记表合并
 * - 点击切换会话，正文区显示对应消息
 * - 未登录只展示登录引导（历史记录与账号绑定）
 */
export function Sidebar({
  activeSessionId,
  isStreaming,
  onNewSession,
}: {
  activeSessionId: string;
  isStreaming: boolean;
  onNewSession: () => void;
}) {
  const { user, userId, switchSession, logout } = useUserSession();
  const navigate = useNavigate();

  const [remote, setRemote] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [localTick, setLocalTick] = useState(0);
  const prevStreaming = useRef(isStreaming);

  const refresh = useCallback(() => {
    setLocalTick((t) => t + 1);
    if (!user || !userId) {
      setRemote([]);
      return;
    }
    setLoading(true);
    sessionApi
      .list({ user_id: userId, limit: 50 })
      .then((r) => setRemote(r.sessions))
      .catch(() => setRemote([]))
      .finally(() => setLoading(false));
  }, [user, userId]);

  // 首次进入 / 登录态或会话切换后刷新
  useEffect(() => {
    refresh();
  }, [refresh, activeSessionId]);

  // 一轮对话结束后刷新（标题与时间由后端更新）
  useEffect(() => {
    if (prevStreaming.current && !isStreaming) refresh();
    prevStreaming.current = isStreaming;
  }, [isStreaming, refresh]);

  // 合并：本地登记表提供"刚发起但后端未收录"的会话，后端提供标题与活跃态
  const merged: SidebarItem[] = (() => {
    if (!user) return [];
    /** 兼容后端可能返回 "userId:sessionId" 形式的会话 ID */
    const clean = (sid: string) => (sid.includes(':') ? sid.split(':').pop()! : sid);
    const map = new Map<string, SidebarItem>();
    remote.forEach((s) => {
      const sid = clean(s.session_id);
      map.set(sid, {
        session_id: sid,
        title: s.title || '问诊会话',
        time: s.last_active_at,
        is_inquiry_active: s.is_inquiry_active,
      });
    });
    if (localTick >= 0) {
      listLocalSessions(userId).forEach((s) => {
        const hit = map.get(s.session_id);
        map.set(s.session_id, {
          session_id: s.session_id,
          title: hit?.title && hit.title !== '问诊会话' ? hit.title : s.title,
          time: hit?.time ?? new Date(s.updated_at).toISOString(),
          is_inquiry_active: hit?.is_inquiry_active ?? false,
        });
      });
    }
    return [...map.values()].sort((a, b) => (b.time || '').localeCompare(a.time || ''));
  })();

  const fmtTime = (t: string | null) => {
    if (!t) return '刚刚';
    const d = dayjs(t);
    if (!d.isValid()) return '';
    const today = dayjs().isSame(d, 'day');
    return today ? d.format('HH:mm') : d.format('MM-DD');
  };

  return (
    <aside className="hidden h-full w-[240px] shrink-0 flex-col border-r border-slate-200 bg-white xl:flex 2xl:w-[280px]">
      {/* 品牌 + 新建 */}
      <div className="flex items-center justify-between px-4 pb-2 pt-4">
        <div className="flex items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary-700">
            <svg viewBox="0 0 24 24" className="h-4 w-4 text-white" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 12h4l2-6 4 12 2-6h6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <span className="text-sm font-semibold tracking-tight text-slate-900">智慧问诊</span>
        </div>
        <button
          onClick={refresh}
          className="flex h-7 w-7 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          aria-label="刷新会话列表"
          title="刷新"
        >
          <RefreshCw className={clsx('h-3.5 w-3.5', loading && 'animate-spin')} />
        </button>
      </div>

      <div className="px-3 pb-3">
        <button
          onClick={onNewSession}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-primary-700 py-2 text-sm font-medium text-white transition-colors hover:bg-primary-800"
        >
          <Plus className="h-4 w-4" /> 新会话
        </button>
      </div>

      {/* 会话列表 */}
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center justify-between px-4 py-2">
          <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">历史会话</span>
          {user && merged.length > 0 && <span className="text-[11px] text-slate-400">{merged.length}</span>}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {!user ? (
            <div className="mx-2 rounded-lg border border-dashed border-slate-200 px-3 py-4 text-center">
              <p className="text-xs leading-relaxed text-slate-400">登录后在此查看历史问诊记录</p>
              <button
                onClick={() => navigate('/login')}
                className="mt-2 w-full rounded-md border border-slate-200 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
              >
                去登录
              </button>
            </div>
          ) : merged.length === 0 ? (
            <div className="px-3 py-6 text-center text-xs leading-relaxed text-slate-400">
              {loading ? '加载中…' : '暂无历史会话，开始一次问诊吧'}
            </div>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {merged.map((s) => {
                const active = s.session_id === activeSessionId;
                return (
                  <li key={s.session_id}>
                    <button
                      onClick={() => switchSession(s.session_id)}
                      className={clsx(
                        'group flex w-full flex-col items-start gap-1 rounded-lg px-3 py-2 text-left transition-colors',
                        active ? 'bg-primary-50' : 'hover:bg-slate-50',
                      )}
                      aria-current={active ? 'page' : undefined}
                    >
                      <div className="flex w-full items-center gap-1.5">
                        <span
                          className={clsx(
                            'min-w-0 flex-1 truncate text-[13px] leading-snug',
                            active ? 'font-medium text-primary-800' : 'text-slate-700',
                          )}
                        >
                          {s.title || '问诊会话'}
                        </span>
                        {s.is_inquiry_active && (
                          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-primary-500" title="问诊中" />
                        )}
                      </div>
                      <span className="text-[11px] text-slate-400">{fmtTime(s.time)}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>

      {/* 用户区 */}
      <div className="border-t border-slate-200 p-2">
        {user ? (
          <div className="flex items-center gap-2 rounded-lg px-2 py-1.5">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-slate-200 text-xs font-medium text-slate-600">
              {user.name.slice(0, 1)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium text-slate-800">{user.name}</div>
              <div className="truncate text-[11px] text-slate-400">{user.phone}</div>
            </div>
            <button
              onClick={() => void logout()}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-slate-400 hover:bg-slate-100 hover:text-slate-600"
              aria-label="退出登录"
              title="退出登录"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          </div>
        ) : (
          <button
            onClick={() => navigate('/login')}
            className="w-full rounded-lg border border-slate-200 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            登录以同步记录
          </button>
        )}
      </div>
    </aside>
  );
}
