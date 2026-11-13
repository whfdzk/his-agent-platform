import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import { ChevronRight, Clock, Stethoscope } from 'lucide-react';
import { sessionApi, type SessionSummary } from '@his/shared';
import { useUserSession } from '../store/session';

/** 后端下发 UTC ISO 时间，直接展示会露出 "2026-11-01T01:34:44+00:00" */
function fmtTime(t: string | null): string {
  if (!t) return '进行中';
  const d = dayjs(t);
  if (!d.isValid()) return '进行中';
  return d.isSame(dayjs(), 'day') ? `今天 ${d.format('HH:mm')}` : d.format('YYYY-MM-DD HH:mm');
}

export function HistoryPage() {
  const { userId } = useUserSession();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!userId) return;
    sessionApi.list({ user_id: userId, limit: 50 })
      .then((r) => setSessions(r.sessions))
      .catch(() => setSessions([]))
      .finally(() => setLoading(false));
  }, [userId]);

  return (
    <div className="h-full overflow-y-auto bg-slate-100">
      <header className="sticky top-0 border-b border-slate-200 bg-white px-4 py-3 text-sm font-semibold tracking-tight text-slate-900">
        问诊记录
      </header>

      {loading ? (
        <div className="p-8 text-center text-sm text-slate-400">加载中…</div>
      ) : sessions.length === 0 ? (
        <div className="flex flex-col items-center gap-2 p-12 text-center">
          <Stethoscope className="h-10 w-10 text-slate-300" />
          <div className="text-sm text-slate-400">暂无问诊记录，去发起一次问诊吧</div>
        </div>
      ) : (
        <div className="mx-auto w-full max-w-2xl px-3 py-3 sm:px-4 2xl:max-w-4xl">
          <ul className="divide-y divide-slate-100 overflow-hidden rounded-xl border border-slate-200 bg-white">
            {sessions.map((s) => (
              <li key={s.session_id}>
                <button
                  onClick={() => navigate(`/history/${s.session_id}`)}
                  className="flex w-full items-center gap-3 px-4 py-3.5 text-left transition-colors hover:bg-slate-50"
                >
                  <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-50 text-primary-700">
                    <Stethoscope className="h-4.5 w-4.5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="truncate text-sm text-slate-800">
                        {s.title || '问诊会话'}
                      </span>
                      {s.is_inquiry_active && (
                        <span className="shrink-0 rounded-full bg-primary-50 px-2 py-0.5 text-[10px] text-primary-700">
                          问诊中
                        </span>
                      )}
                    </div>
                    <div className="mt-0.5 flex items-center gap-1 text-xs text-slate-400">
                      <Clock className="h-3 w-3" />
                      {fmtTime(s.last_active_at)}
                    </div>
                  </div>
                  <ChevronRight className="h-4 w-4 shrink-0 text-slate-300" />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
