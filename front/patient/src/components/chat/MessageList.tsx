import { useState } from 'react';
import { motion } from 'framer-motion';
import { CalendarCheck2, ShieldAlert, ThumbsDown, ThumbsUp } from 'lucide-react';
import clsx from 'clsx';
import { knowledgeApi, type ChatMessage } from '@his/shared';

import { useUserSession } from '../../store/session';

/** 轻量渲染：把 **text** 转成加粗（不注入 HTML，避免 XSS） */
function renderRich(text: string) {
  const parts = text.split('**');
  if (parts.length === 1) return text;
  return parts.map((p, i) =>
    i % 2 === 1 ? (
      <strong key={i} className="font-semibold text-slate-900">
        {p}
      </strong>
    ) : (
      p
    ),
  );
}

/** 回答满意度：写入 knowledge_feedback，供知识库迭代使用 */
function FeedbackBar({ message, question }: { message: ChatMessage; question: string }) {
  const { user } = useUserSession();
  const [rating, setRating] = useState<1 | -1 | null>(null);
  const [sending, setSending] = useState(false);

  const submit = async (value: 1 | -1) => {
    if (rating || sending) return;
    setSending(true);
    try {
      await knowledgeApi.submitFeedback({
        question: question.slice(0, 500),
        answer: message.content.slice(0, 500),
        rating: value,
      });
      setRating(value);
    } catch {
      // 反馈属于旁路能力，失败不打扰用户
    } finally {
      setSending(false);
    }
  };

  if (rating) {
    return (
      <div className="flex items-center gap-1.5 pl-1 text-[11px] text-emerald-600">
        <ThumbsUp className="h-3 w-3" />
        已记录，感谢反馈
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5 pl-1 text-[11px] text-slate-400">
      <span>这条回答有帮助吗</span>
      <button
        type="button"
        disabled={sending}
        onClick={() => void submit(1)}
        aria-label="回答有帮助"
        className="rounded-md p-1 transition-colors hover:bg-slate-100 hover:text-emerald-600 disabled:opacity-50"
      >
        <ThumbsUp className="h-3.5 w-3.5" />
      </button>
      <button
        type="button"
        disabled={sending}
        onClick={() => void submit(-1)}
        aria-label="回答没帮助"
        className="rounded-md p-1 transition-colors hover:bg-slate-100 hover:text-rose-500 disabled:opacity-50"
      >
        <ThumbsDown className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

function Bubble({ message, question }: { message: ChatMessage; question: string }) {
  const isUser = message.role === 'user';
  const { user } = useUserSession();

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      className={clsx('flex items-start gap-2.5', isUser ? 'flex-row-reverse' : 'flex-row')}
    >
      {/* 头像：用户显示姓名首字，助手用品牌标 */}
      {isUser ? (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-200 text-xs font-medium text-slate-600">
          {user ? user.name.slice(0, 1) : '我'}
        </div>
      ) : (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary-700">
          <svg viewBox="0 0 24 24" className="h-4 w-4 text-white" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M3 12h4l2-6 4 12 2-6h6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
      )}

      <div className="flex max-w-[76%] min-w-0 flex-col gap-1">
        <div
          className={clsx(
            'px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap break-words',
            isUser && 'rounded-xl rounded-tr-sm bg-primary-700 text-white',
            message.role === 'assistant' && 'rounded-xl rounded-tl-sm border border-slate-200 bg-white text-slate-800',
            message.role === 'error' && 'rounded-xl rounded-tl-sm border border-red-200 bg-red-50 text-red-700',
          )}
        >
          {message.role === 'assistant' && message.content
            ? renderRich(message.content)
            : message.content}
          {!message.content && message.streaming && <TypingDots />}
          {message.streaming && message.content && (
            <span className="ml-0.5 inline-block h-4 w-0.5 animate-pulse bg-slate-400 align-middle" />
          )}
          {message.stopped && <span className="ml-2 text-xs text-slate-400">已停止</span>}
          {message.safetyNotice && (
            <div className="mt-2 flex items-start gap-1.5 rounded-lg bg-amber-50 px-2.5 py-1.5 text-[11px] leading-relaxed text-amber-700">
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{message.safetyNotice}</span>
            </div>
          )}
        </div>
        {message.role === 'assistant' && !message.streaming && message.content && (
          <FeedbackBar message={message} question={question} />
        )}
      </div>
    </motion.div>
  );
}

function HandoffCard({ message }: { message: ChatMessage }) {
  const a = message.appointment!;
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="ml-10 max-w-[85%] overflow-hidden rounded-xl border border-slate-200 bg-white"
    >
      <div className="flex items-center gap-2 border-b border-slate-100 bg-primary-50 px-4 py-2.5">
        <CalendarCheck2 className="h-4 w-4 text-primary-700" />
        <span className="text-sm font-medium text-primary-800">挂号成功</span>
        <span className="ml-auto font-mono text-xs text-slate-400">{a.appointment_no}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 px-4 py-3 text-sm">
        <span className="text-slate-400">科室</span><span className="text-slate-800">{a.department}</span>
        <span className="text-slate-400">医生</span><span className="text-slate-800">{a.doctor_name}</span>
        <span className="text-slate-400">时间</span>
        <span className="text-slate-800">{a.date} {a.time_slot}</span>
        <span className="text-slate-400">序号</span><span className="text-slate-800">第 {a.queue_number} 号</span>
        <span className="text-slate-400">挂号费</span><span className="text-slate-800">¥{a.fee}</span>
      </div>
      {a.diagnosis && (
        <div className="border-t border-slate-100 px-4 py-2 text-xs text-slate-500">
          诊断参考：{a.diagnosis}
        </div>
      )}
    </motion.div>
  );
}

export function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-1">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400"
          style={{ animationDelay: `${i * 0.15}s` }}
        />
      ))}
    </span>
  );
}

export function MessageList({
  messages,
}: {
  messages: ChatMessage[];
}) {
  return (
    <div className="flex flex-col gap-3.5 px-4 py-4">
      {messages.map((m, idx) => {
        if (m.role === 'handoff') return <HandoffCard key={m.id} message={m} />;
        // 反馈要带上对应的提问：向上找最近一条用户消息（遇到上一条助手回复即停）
        let question = '';
        if (m.role === 'assistant') {
          for (let i = idx - 1; i >= 0; i--) {
            if (messages[i].role === 'user') {
              question = messages[i].content;
              break;
            }
            if (messages[i].role === 'assistant') break;
          }
        }
        return <Bubble key={m.id} message={m} question={question} />;
      })}
    </div>
  );
}
