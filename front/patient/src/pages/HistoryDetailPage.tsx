import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import clsx from 'clsx';
import { sessionApi, type HistoryMessage } from '@his/shared';
import { useUserSession } from '../store/session';

export function HistoryDetailPage() {
  const { sessionId = '' } = useParams();
  const { userId } = useUserSession();
  const navigate = useNavigate();
  const [messages, setMessages] = useState<HistoryMessage[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!userId || !sessionId) return;
    sessionApi.messages(sessionId, userId)
      .then((r) => setMessages(r.messages))
      .catch(() => setMessages([]))
      .finally(() => setLoading(false));
  }, [userId, sessionId]);

  return (
    <div className="flex h-full flex-col bg-slate-100">
      <header className="flex items-center gap-2 border-b border-slate-200 bg-white px-4 py-3">
        <button onClick={() => navigate(-1)} className="text-slate-500 hover:text-slate-700">
          <ArrowLeft className="h-5 w-5" />
        </button>
        <span className="text-sm font-semibold tracking-tight text-slate-900">对话记录</span>
        <span className="ml-auto font-mono text-xs text-slate-300">{sessionId.slice(0, 8)}</span>
      </header>

      <div className="mx-auto w-full max-w-2xl flex-1 overflow-y-auto px-4 py-4 2xl:max-w-4xl">
        {loading ? (
          <div className="text-center text-sm text-slate-400">加载中…</div>
        ) : messages.length === 0 ? (
          <div className="pt-16 text-center text-sm text-slate-400">
            暂无历史消息（问诊进行中的消息会在结束后可回放）
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {messages.map((m, i) => (
              <div
                key={i}
                className={clsx(
                  'max-w-[80%] rounded-xl px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap',
                  m.role === 'user'
                    ? 'ml-auto rounded-tr-sm bg-primary-700 text-white'
                    : 'rounded-tl-sm border border-slate-200 bg-white text-slate-800',
                )}
              >
                {m.content}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
