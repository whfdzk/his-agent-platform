import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Eye, MessagesSquare, Search, ThumbsDown, ThumbsUp, X } from 'lucide-react';
import clsx from 'clsx';
import {
  conversationAdminApi,
  type AdminConversation,
  type AdminFeedbackItem,
  type AdminMessageItem,
} from '@his/shared';

type Tab = 'conversations' | 'feedback';

export function ConversationsPage() {
  const [tab, setTab] = useState<Tab>('conversations');
  const [conversations, setConversations] = useState<AdminConversation[]>([]);
  const [feedback, setFeedback] = useState<AdminFeedbackItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  const [viewing, setViewing] = useState<number | null>(null);

  const load = (p = page, kw = keyword) => {
    setLoading(true);
    const req =
      tab === 'conversations'
        ? conversationAdminApi.list({ keyword: kw || undefined, page: p, page_size: 20 })
        : conversationAdminApi.feedback({ page: p, page_size: 20 });
    req
      .then((r) => {
        if (tab === 'conversations') {
          setConversations((r as { conversations: AdminConversation[] }).conversations);
          setTotal((r as { total: number }).total);
        } else {
          setFeedback((r as { feedback: AdminFeedbackItem[] }).feedback);
          setTotal((r as { total: number }).total);
        }
        setPage(p);
      })
      .catch(() => {
        setConversations([]);
        setFeedback([]);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  const totalPages = Math.max(Math.ceil(total / 20), 1);

  const closeConversation = async (c: AdminConversation) => {
    if (!confirm(`关闭会话「${c.title || c.session_id}」？`)) return;
    await conversationAdminApi.updateStatus(c.id, 'closed').catch((e) => alert(e.message));
    load();
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-lg font-medium">对话管理</h1>
        <p className="mt-0.5 text-xs text-gray-400">对话记录查看、异常会话处置与用户反馈收集 · 共 {total} 条</p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex gap-1 rounded-xl border border-gray-200 bg-white p-1">
          <button
            onClick={() => setTab('conversations')}
            className={clsx('rounded-lg px-4 py-1.5 text-sm', tab === 'conversations' ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50')}
          >
            对话记录
          </button>
          <button
            onClick={() => setTab('feedback')}
            className={clsx('rounded-lg px-4 py-1.5 text-sm', tab === 'feedback' ? 'bg-primary-500 text-white' : 'text-gray-600 hover:bg-gray-50')}
          >
            用户反馈
          </button>
        </div>
        {tab === 'conversations' && (
          <>
            <div className="relative w-full sm:w-auto">
              <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-300" />
              <input
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && load(1)}
                placeholder="搜索标题 / 会话 ID"
                className="w-full rounded-xl border border-gray-200 bg-white py-2 pl-9 pr-3 text-sm outline-none focus:ring-2 focus:ring-primary-100 sm:w-64"
              />
            </div>
            <button onClick={() => load(1)} className="rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600">
              查询
            </button>
          </>
        )}
      </div>

      {loading ? (
        <div className="rounded-2xl border border-gray-100 bg-white p-10 text-center text-sm text-gray-400">加载中…</div>
      ) : tab === 'conversations' ? (
        conversations.length === 0 ? (
          <Empty icon={MessagesSquare} text="暂无会话" />
        ) : (
          <div className="overflow-x-auto rounded-2xl border border-gray-100 bg-white">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                  <th className="px-4 py-3 font-normal">标题</th>
                  <th className="px-4 py-3 font-normal">用户</th>
                  <th className="px-4 py-3 font-normal">消息数</th>
                  <th className="px-4 py-3 font-normal">状态</th>
                  <th className="px-4 py-3 font-normal">最后活跃</th>
                  <th className="px-4 py-3 text-right font-normal">操作</th>
                </tr>
              </thead>
              <tbody>
                {conversations.map((c) => (
                  <tr key={c.id} className="border-b border-gray-50 last:border-0 hover:bg-gray-50/60">
                    <td className="max-w-64 truncate px-4 py-3 text-gray-700">{c.title || c.session_id}</td>
                    <td className="px-4 py-3 text-gray-500">{c.user_id}</td>
                    <td className="px-4 py-3 text-gray-500">{c.message_count}</td>
                    <td className="px-4 py-3">
                      <span className={clsx(
                        'rounded-full px-2 py-0.5 text-[10px]',
                        c.status === 'active' ? 'bg-emerald-50 text-emerald-600' : 'bg-gray-100 text-gray-400',
                      )}>
                        {c.status === 'active' ? '进行中' : '已关闭'}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-400">{c.last_active_at?.slice(0, 16).replace('T', ' ') || '-'}</td>
                    <td className="px-4 py-3">
                      <div className="flex justify-end gap-1">
                        <button
                          onClick={() => setViewing(c.id)}
                          className="flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-gray-500 hover:bg-gray-100"
                        >
                          <Eye className="h-3 w-3" /> 查看
                        </button>
                        {c.status === 'active' && (
                          <button
                            onClick={() => closeConversation(c)}
                            className="rounded-lg px-2 py-1 text-xs text-red-500 hover:bg-red-50"
                          >
                            关闭
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : feedback.length === 0 ? (
        <Empty icon={ThumbsUp} text="暂无反馈" />
      ) : (
        <div className="space-y-3">
          {feedback.map((f) => (
            <div key={f.id} className="rounded-2xl border border-gray-100 bg-white p-4">
              <div className="mb-1.5 flex items-center gap-2 text-xs text-gray-400">
                {f.rating === 1 ? (
                  <span className="flex items-center gap-1 text-emerald-600"><ThumbsUp className="h-3 w-3" /> 有用</span>
                ) : f.rating === -1 ? (
                  <span className="flex items-center gap-1 text-red-500"><ThumbsDown className="h-3 w-3" /> 无用</span>
                ) : (
                  <span>未评价</span>
                )}
                <span>用户 {f.user_id}</span>
                {f.intent && <span className="rounded-full bg-gray-100 px-2 py-0.5">{f.intent}</span>}
                <span className="ml-auto">{f.created_at?.slice(0, 16).replace('T', ' ') || ''}</span>
              </div>
              <div className="text-sm text-gray-700">Q：{f.question}</div>
              <div className="mt-1 line-clamp-2 text-xs text-gray-400">A：{f.answer || '—'}</div>
              {f.comment && <div className="mt-1.5 rounded-xl bg-gray-50 px-3 py-2 text-xs text-gray-500">反馈：{f.comment}</div>}
            </div>
          ))}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-2 text-sm">
          <button disabled={page <= 1} onClick={() => load(page - 1)} className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 disabled:opacity-40">
            上一页
          </button>
          <span className="text-gray-400">{page} / {totalPages}</span>
          <button disabled={page >= totalPages} onClick={() => load(page + 1)} className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 disabled:opacity-40">
            下一页
          </button>
        </div>
      )}

      <MessagesDrawer id={viewing} onClose={() => setViewing(null)} />
    </div>
  );
}

function MessagesDrawer({ id, onClose }: { id: number | null; onClose: () => void }) {
  const [conversation, setConversation] = useState<AdminConversation | null>(null);
  const [messages, setMessages] = useState<AdminMessageItem[]>([]);

  useEffect(() => {
    if (id) {
      conversationAdminApi
        .messages(id)
        .then((r) => {
          setConversation(r.conversation);
          setMessages(r.messages);
        })
        .catch(() => {
          setConversation(null);
          setMessages([]);
        });
    }
  }, [id]);

  if (id === null) return null;

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex justify-end bg-black/40"
        onClick={onClose}
      >
        <motion.div
          initial={{ x: 80 }}
          animate={{ x: 0 }}
          className="flex h-full w-full max-w-xl flex-col bg-white shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between border-b border-gray-100 px-6 py-4">
            <div className="min-w-0">
              <h3 className="truncate text-base font-medium">{conversation?.title || conversation?.session_id || '会话详情'}</h3>
              <div className="text-xs text-gray-400">
                用户 {conversation?.user_id} · {messages.length} 条消息
              </div>
            </div>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>
          <div className="flex-1 space-y-3 overflow-y-auto p-6">
            {messages.map((m) => (
              <div key={m.id} className={clsx('flex', m.role === 'user' ? 'justify-end' : 'justify-start')}>
                <div
                  className={clsx(
                    'max-w-[80%] rounded-2xl px-3.5 py-2.5 text-sm',
                    m.role === 'user'
                      ? 'bg-primary-500 text-white'
                      : m.role === 'system'
                        ? 'bg-gray-100 text-gray-500'
                        : 'bg-gray-50 text-gray-700',
                  )}
                >
                  {m.event_type !== 'token' && (
                    <div className="mb-0.5 text-[10px] opacity-70">[{m.event_type}]</div>
                  )}
                  <div className="whitespace-pre-wrap break-words">{m.content || '（空）'}</div>
                  <div className="mt-1 text-[10px] opacity-50">{m.created_at?.slice(5, 16).replace('T', ' ') || ''}</div>
                </div>
              </div>
            ))}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function Empty({ icon: Icon, text }: { icon: typeof MessagesSquare; text: string }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-2xl border border-gray-100 bg-white p-12 text-center">
      <Icon className="h-10 w-10 text-gray-300" />
      <div className="text-sm text-gray-400">{text}</div>
    </div>
  );
}
