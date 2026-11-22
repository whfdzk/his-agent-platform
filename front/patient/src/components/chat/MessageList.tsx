import { useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { BookOpen, CalendarCheck2, CheckCircle2, ChevronDown, Headset, Loader2, ShieldAlert, Square, ThumbsDown, ThumbsUp, Volume2 } from 'lucide-react';
import clsx from 'clsx';
import { appointmentApi, knowledgeApi, clinicalActionApi, type AppointmentData, type ChatMessage, type CitationRef, type ConfirmRequestData } from '@his/shared';

import { useUserSession } from '../../store/session';
import { synthesizeSpeech } from '../../lib/voiceApi';

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

/** 朗读这条回复：按需请求后端 TTS，播完即释放 blob URL */
function SpeakButton({ text }: { text: string }) {
  const [busy, setBusy] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [errorText, setErrorText] = useState('');
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);

  const release = () => {
    audioRef.current?.pause();
    audioRef.current = null;
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
    setPlaying(false);
  };

  useEffect(() => () => release(), []);

  const toggle = async () => {
    if (playing) {
      release();
      return;
    }
    setBusy(true);
    setErrorText('');
    try {
      const blob = await synthesizeSpeech(text);
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audioRef.current = audio;
      urlRef.current = url;
      audio.onended = release;
      await audio.play();
      setPlaying(true);
    } catch (e) {
      setErrorText(e instanceof Error ? e.message : '朗读失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex items-center gap-1.5 pl-1">
      <button
        type="button"
        onClick={() => void toggle()}
        disabled={busy}
        aria-label={playing ? '停止朗读' : '朗读这条回复'}
        className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-slate-400 transition-colors hover:bg-slate-100 hover:text-primary-700 disabled:opacity-50"
      >
        {busy ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : playing ? (
          <Square className="h-3 w-3 fill-current" />
        ) : (
          <Volume2 className="h-3.5 w-3.5" />
        )}
        <span>{busy ? '合成中' : playing ? '停止' : '朗读'}</span>
      </button>
      {errorText && <span className="text-[11px] text-amber-600">{errorText}</span>}
    </div>
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

/**
 * 引用来源：回答正文里出现"指南建议/说明书用法"时，让患者能点开核对出处。
 * 默认折叠，避免把每条回复撑长；页码为 0 表示该资料没有页码（纯文本指南），不展示页码。
 */
function CitationList({ items }: { items: CitationRef[] }) {
  const [open, setOpen] = useState(false);
  if (!items.length) return null;
  return (
    <div className="mt-2 border-t border-slate-100 pt-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1 text-[11px] text-slate-400 transition-colors hover:text-primary-700"
      >
        <BookOpen className="h-3 w-3" />
        参考资料 {items.length} 篇
        <ChevronDown className={clsx('h-3 w-3 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <ol className="mt-1.5 flex flex-col gap-1.5">
          {items.map((c, i) => (
            <li
              key={`${c.doc_name}-${c.chunk_index ?? i}`}
              className="rounded-md bg-slate-50 px-2 py-1.5 text-[11px] leading-relaxed text-slate-500"
            >
              <span className="mr-1 text-slate-400">[{i + 1}]</span>
              <span className="font-medium text-slate-700">{c.doc_name || '未命名资料'}</span>
              {c.page_number ? <span className="text-slate-400"> · 第 {c.page_number} 页</span> : null}
              {c.quote && <div className="mt-0.5 text-slate-400">{c.quote}</div>}
            </li>
          ))}
        </ol>
      )}
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
      ) : message.fromAgent === 'human_agent' ? (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-600" title="人工坐席">
          <Headset className="h-4 w-4 text-white" />
        </div>
      ) : message.fromAgent === 'handoff_close' ? (
        <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-slate-300" title="系统收尾">
          <CheckCircle2 className="h-4 w-4 text-white" />
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
            message.fromAgent === 'human_agent' && 'rounded-xl rounded-tl-sm border border-emerald-200 bg-emerald-50/70 text-slate-800',
            message.fromAgent === 'handoff_close' && 'rounded-xl rounded-tl-sm border border-dashed border-slate-300 bg-slate-50 text-slate-600',
            message.role === 'error' && 'rounded-xl rounded-tl-sm border border-red-200 bg-red-50 text-red-700',
          )}
        >
          {(message.fromAgent === 'human_agent' || message.fromAgent === 'handoff_close') && (
            <div className="mb-1 flex items-center gap-1 text-[11px] font-medium">
              {message.fromAgent === 'human_agent' ? (
                <>
                  <Headset className="h-3 w-3 text-emerald-600" />
                  <span className="text-emerald-700">人工坐席</span>
                </>
              ) : (
                <span className="text-slate-500">人工环节结束 · 系统记录</span>
              )}
            </div>
          )}
          {message.imageUrl && (
            <img
              src={message.imageUrl}
              alt="上传的图片"
              className="mb-2 max-h-52 w-full max-w-[220px] rounded-lg object-contain"
            />
          )}
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
          {message.role === 'assistant' && message.citations?.length ? (
            <CitationList items={message.citations} />
          ) : null}
        </div>
        {message.confirm && <ConfirmCard data={message.confirm} />}
        {message.role === 'assistant' && !message.streaming && message.content && (
          <div className="flex flex-wrap items-center gap-2">
            <SpeakButton text={message.content} />
            {/* 坐席原话不该进 AI 知识反馈（那是真人说的，不是模型答的） */}
            {!message.fromAgent && <FeedbackBar message={message} question={question} />}
          </div>
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

/**
 * B2：高危动作确认卡片（SSE confirm_request 下发）。
 *
 * 后端下发时只落了 pending 草稿、号源并未占用；点「确认」才会走执行器建单。
 * 所以卡片必须自己说清"现在还没约上"，按钮也只在 pending 状态出现。
 * 超时（默认 15 分钟）后点确认会收到 410，卡片转成"已失效"并提示重新发起，
 * 而不是静默失败让患者以为约上了。
 */
interface DoctorOption {
  clinician_id: number;
  name: string;
  title: string;
  specialty?: string;
  departments: { id: number; name: string; is_primary: boolean }[];
}

function ConfirmCard({ data }: { data: ConfirmRequestData }) {
  const [state, setState] = useState<'pending' | 'busy' | 'approved' | 'rejected' | 'expired'>('pending');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AppointmentData | null>(null);
  const isAppointment = data.action_type === 'appointment' || data.action_type === 'handoff';

  // C4 收尾：改选医生只改草稿，卡片的 summary/preview 都跟着后端回传刷新，
  // 否则正文还写着旧医生名，和下面网格里的新医生自相矛盾。
  const [preview, setPreview] = useState<NonNullable<ConfirmRequestData['preview']>>(data.preview ?? {});
  const [summary, setSummary] = useState(data.summary);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerBusy, setPickerBusy] = useState(false);
  const [options, setOptions] = useState<DoctorOption[] | null>(null);
  const [selected, setSelected] = useState<number>(data.preview?.doctor_clinician_id ?? 0);
  const p = preview;

  // 恢复草稿时（restorePending）会把"同一位置的消息"换成另一条动作，
  // React 复用同一节点、组件不重挂载 —— 若本地 state 不跟着 data.id 重置，
  // 卡片会出现"标题是新动作 #127、摘要还是旧动作的日期"这种串台。
  // 这里只在动作 id 变化时重置，正常 SSE 每条消息独立，不会触发。
  const actionId = data.id;
  useEffect(() => {
    setState('pending');
    setError(null);
    setResult(null);
    setPreview(data.preview ?? {});
    setSummary(data.summary);
    setPickerOpen(false);
    setOptions(null);
    setSelected(data.preview?.doctor_clinician_id ?? 0);
  }, [actionId]);

  const openPicker = async () => {
    setPickerOpen(true);
    setError(null);
    setSelected(p.doctor_clinician_id ?? 0);
    if (options) return;
    setPickerBusy(true);
    try {
      const resp = await appointmentApi.doctors();
      const all: DoctorOption[] = resp.doctors ?? [];
      // 只在本科室目录里换人：跨科室改选会把患者挂到别的科，后端也会拒绝
      setOptions(all.filter((d) => !p.department || d.departments.some((x) => x.name === p.department)));
    } catch (e) {
      setError((e as { message?: string }).message || '医生名单加载失败，请稍后重试');
      setOptions([]);
    } finally {
      setPickerBusy(false);
    }
  };

  const applyDoctor = async () => {
    setPickerBusy(true);
    setError(null);
    try {
      const resp = await clinicalActionApi.retargetDoctor(data.id, selected);
      const item = resp.item;
      if (item) {
        setPreview(item.preview ?? {});
        setSummary(item.summary);
      }
      setPickerOpen(false);
    } catch (e) {
      const err = e as { status?: number; message?: string };
      if (err.status === 410) {
        setPickerOpen(false);
        setState('expired');
        setError('该确认已超时失效，号源未被占用，请重新发起预约。');
        return;
      }
      setError(err.message || '改选医生失败，请稍后重试');
    } finally {
      setPickerBusy(false);
    }
  };

  const decide = async (decision: 'approve' | 'reject') => {
    setState('busy');
    setError(null);
    try {
      if (decision === 'approve') {
        const resp = await clinicalActionApi.approve(data.id);
        setResult(resp.data?.appointment_no ? resp.data : null);
        setState('approved');
      } else {
        await clinicalActionApi.reject(data.id);
        setState('rejected');
      }
    } catch (e) {
      const err = e as { status?: number; message?: string };
      if (err.status === 410) {
        setState('expired');
        setError('该确认已超时失效，号源未被占用，请重新发起预约。');
        return;
      }
      setState('pending');
      setError(err.message || '操作失败，请稍后重试');
    }
  };

  const headerText =
    state === 'approved' ? '已确认' :
    state === 'rejected' ? '已取消' :
    state === 'expired' ? '已失效' : '待您确认';

  return (
    <div className="mt-1 overflow-hidden rounded-xl border border-amber-200 bg-white">
      <div className="flex items-center gap-2 border-b border-amber-100 bg-amber-50 px-3 py-2">
        <ShieldAlert className="h-4 w-4 text-amber-600" />
        <span className="text-sm font-medium text-amber-800">{headerText}</span>
        <span className="ml-auto font-mono text-[11px] text-slate-400">#{data.id}</span>
      </div>

      <div className="px-3 py-2.5 text-sm leading-relaxed text-slate-700">{summary}</div>

      {(p.department || p.date || p.time_slot || p.doctor_name) && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1 px-3 pb-2 text-xs">
          {p.department && (<><span className="text-slate-400">科室</span><span className="text-slate-700">{p.department}</span></>)}
          {isAppointment && (
            <>
              <span className="text-slate-400">医生</span>
              <span className="text-slate-700">{p.doctor_name || '待分配'}</span>
            </>
          )}
          {(p.date || p.time_slot) && (
            <><span className="text-slate-400">时间</span><span className="text-slate-700">{p.date} {p.time_slot}</span></>
          )}
          {p.fee !== undefined && (<><span className="text-slate-400">挂号费</span><span className="text-slate-700">¥{p.fee}</span></>)}
        </div>
      )}

      {pickerOpen && state === 'pending' && (
        <div className="border-t border-slate-100 bg-slate-50/60 px-3 py-2">
          <div className="mb-1.5 text-[11px] text-slate-500">
            {p.department || '本科室'} 可挂号医生
          </div>
          {pickerBusy && options === null ? (
            <div className="flex items-center gap-2 py-1 text-xs text-slate-500">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              正在加载医生名单…
            </div>
          ) : (
            <select
              value={selected}
              onChange={(e) => setSelected(Number(e.target.value))}
              disabled={pickerBusy}
              className="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-700 focus:border-primary-500 focus:outline-none disabled:opacity-60"
            >
              <option value={0}>暂不指定（到院分配）</option>
              {(options ?? []).map((o) => (
                <option key={o.clinician_id} value={o.clinician_id}>
                  {`${o.name} ${o.title}${o.specialty ? `（${o.specialty}）` : ''}`}
                </option>
              ))}
            </select>
          )}
          {options !== null && options.length === 0 && !pickerBusy && (
            <div className="mt-1 text-[11px] text-slate-400">本科室暂无可挂号医生，只能先按待分配处理。</div>
          )}
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              onClick={applyDoctor}
              disabled={pickerBusy || options === null}
              className="rounded-lg bg-primary-700 px-2.5 py-1 text-[11px] font-medium text-white transition-colors hover:bg-primary-800 disabled:opacity-60"
            >
              保存
            </button>
            <button
              type="button"
              onClick={() => { setPickerOpen(false); setError(null); setSelected(p.doctor_clinician_id ?? 0); }}
              className="rounded-lg border border-slate-200 px-2.5 py-1 text-[11px] text-slate-600 transition-colors hover:bg-white"
            >
              取消
            </button>
            <span className="text-[11px] text-slate-400">改选只改这张单，还没占号</span>
          </div>
        </div>
      )}

      {state === 'pending' && (
        <div className="border-t border-slate-100 px-3 py-2 text-[11px] text-slate-400">
          现在还没有真正{isAppointment ? '挂号' : '执行'}，点确认后才占用号源。
        </div>
      )}

      {state === 'approved' && (
        <div className="border-t border-emerald-100 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
          {result?.appointment_no
            ? `已为您成功预约，预约号 ${result.appointment_no}，请提前 15 分钟到诊室等候。`
            : '已确认，操作已完成。'}
        </div>
      )}

      {(state === 'rejected' || state === 'expired') && (
        <div className="border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
          {error || '已取消，本次未生成预约。'}
        </div>
      )}

      {error && state === 'pending' && (
        <div className="border-t border-red-100 bg-red-50 px-3 py-2 text-xs text-red-700">{error}</div>
      )}

      {state === 'pending' && (
        <div className="flex items-center gap-2 border-t border-slate-100 px-3 py-2">
          <button
            type="button"
            onClick={() => decide('approve')}
            className="rounded-lg bg-primary-700 px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-primary-800"
          >
            {isAppointment ? '确认预约' : '确认执行'}
          </button>
          <button
            type="button"
            onClick={() => decide('reject')}
            className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 transition-colors hover:bg-slate-50"
          >
            暂不{isAppointment ? '预约' : '执行'}
          </button>
          {isAppointment && p.department && !pickerOpen && (
            <button
              type="button"
              onClick={openPicker}
              className="ml-auto text-xs text-primary-700 underline-offset-2 hover:underline"
            >
              改选医生
            </button>
          )}
        </div>
      )}

      {state === 'busy' && (
        <div className="flex items-center gap-2 border-t border-slate-100 px-3 py-2 text-xs text-slate-500">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          正在处理…
        </div>
      )}
    </div>
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
