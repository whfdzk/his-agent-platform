import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Headset, Plus, RefreshCw } from 'lucide-react';
import { sessionApi } from '@his/shared';
import type { ChatMessage } from '@his/shared';

import { useUserSession } from '../store/session';
import {
  useChatStream,
  useBackgroundSessions,
  hasLocalMessages,
  seedSessionMessages,
} from '../hooks/useChatStream';
import type { ChatImageAttachment } from '../hooks/useChatStream';
import { usePatientHandoff } from '../hooks/usePatientHandoff';
import { useVoiceInput } from '../hooks/useVoiceInput';
import { recordSession } from '../lib/sessionRegistry';
import { ImagePrepareError, prepareChatImage } from '../lib/imageUpload';
import { Sidebar } from '../components/Sidebar';
import { MessageList } from '../components/chat/MessageList';
import { ChatInput } from '../components/chat/ChatInput';
import { ClarifyModal } from '../components/chat/ClarifyModal';
import { HandoffBar } from '../components/chat/HandoffBar';

export function ChatPage() {
  const { user, userId, sessionId, newSession, switchSession, logout } = useUserSession();
  const navigate = useNavigate();

  const {
    messages,
    handoffCards,
    isStreaming,
    clarifyQuestion,
    sendMessage,
    retryLast,
    stop,
    dismissClarify,
    restorePending,
  } = useChatStream(userId, sessionId);

  const backgroundSessions = useBackgroundSessions(sessionId);

  // C3：当前会话的人工工单（状态条 + 坐席回复实时插入）
  const handoff = usePatientHandoff(userId, sessionId);
  /** 转人工时带给坐席的摘要：用患者最近一句话，坐席不用翻完整对话 */
  const lastAsk = [...messages].reverse().find((m) => m.role === 'user')?.content ?? '';

  const [input, setInput] = useState('');
  // 待发送图片：选图后先压缩成 data URL，随本次消息一起发给后端做图像理解
  const [pendingImage, setPendingImage] = useState<ChatImageAttachment | null>(null);
  const [imageError, setImageError] = useState('');
  const bottomRef = useRef<HTMLDivElement>(null);
  const { voiceState, interim, toggle, errorMsg: voiceError } = useVoiceInput((text) => setInput(text));

  /** 未登录点"新会话"时按钮旁的提示 */
  const [loginHint, setLoginHint] = useState(false);
  const hintTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const showLoginHint = useCallback(() => {
    setLoginHint(true);
    if (hintTimer.current) clearTimeout(hintTimer.current);
    hintTimer.current = setTimeout(() => setLoginHint(false), 3000);
  }, []);

  // 卸载时清掉提示定时器：否则组件已销毁仍会 setState
  useEffect(
    () => () => {
      if (hintTimer.current) clearTimeout(hintTimer.current);
    },
    [],
  );

  /** 顶部用户菜单 */
  const [userMenuOpen, setUserMenuOpen] = useState(false);

  const handleNewSession = () => {
    if (!user) {
      showLoginHint();
      return;
    }
    newSession();
  };

  /** 从侧栏点历史会话：未登录直接引导登录 */
  const handleSidebarNewSession = () => {
    if (!user) {
      navigate('/login');
      return;
    }
    newSession();
  };

  // 切换到本地无内容的会话时，从后端拉取历史消息填充正文
  // 未登录（游客）没有历史记录，不请求后端（避免 401 与无效请求）
  //
  // B2：历史铺好之后再恢复待确认卡片 —— 卡片挂到最后一条助手消息上，
  // 若在消息列表还是空的时候恢复，会先 push 一条空气泡再被历史覆盖。
  useEffect(() => {
    if (!user || !userId || !sessionId) return;
    let cancelled = false;
    const restore = () => {
      if (!cancelled) void restorePending();
    };

    if (hasLocalMessages(sessionId)) {
      restore();
      return () => {
        cancelled = true;
      };
    }
    sessionApi
      .messages(sessionId, userId)
      .then((r) => {
        if (cancelled) return;
        seedSessionMessages(sessionId, r.messages);
        restore();
      })
      .catch(() => restore());
    return () => {
      cancelled = true;
    };
  }, [user, userId, sessionId, restorePending]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, handoffCards]);

  const allMessages: ChatMessage[] = [...messages, ...handoffCards];
  const hasError = messages.length > 0 && messages[messages.length - 1].role === 'error';

  const handlePickImage = useCallback(async (file: File) => {
    setImageError('');
    try {
      setPendingImage(await prepareChatImage(file));
    } catch (e) {
      setPendingImage(null);
      setImageError(e instanceof ImagePrepareError ? e.message : '图片处理失败，请换一张重试');
    }
  }, []);

  const handleSend = () => {
    const text = input;
    const image = pendingImage;
    if (!text.trim() && !image) return;
    setInput('');
    setPendingImage(null);
    setImageError('');
    // 登录用户才登记到本地会话索引（游客不留历史记录）
    if (user && messages.length === 0) {
      recordSession(userId, sessionId, text.trim() || '（图片咨询）');
    }
    void sendMessage(text, undefined, image ?? undefined);
  };

  return (
    <div className="flex h-full min-h-0 bg-slate-100">
      {/* 左侧：历史会话 */}
      <Sidebar
        activeSessionId={sessionId}
        isStreaming={isStreaming}
        onNewSession={handleSidebarNewSession}
      />

      {/* 右侧：当前会话正文 */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* 顶栏 */}
        <header className="relative z-20 flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary-700 xl:hidden">
              <svg viewBox="0 0 24 24" className="h-4.5 w-4.5 text-white" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M3 12h4l2-6 4 12 2-6h6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </div>
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold tracking-tight text-slate-900">
                {user ? `${user.name}的问诊` : '智慧问诊'}
              </div>
              <div className="truncate text-[11px] leading-tight text-slate-400">
                {isStreaming
                  ? '正在生成回复…'
                  : user
                    ? '描述症状，帮你分诊挂号'
                    : '未登录 · 本次对话不会被保存'}
              </div>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {/* 后台进行中的会话 */}
            {backgroundSessions.length > 0 && (
              <button
                onClick={() => switchSession(backgroundSessions[0])}
                className="flex items-center gap-1.5 rounded-lg border border-primary-100 bg-primary-50 px-2.5 py-1.5 text-xs text-primary-700 hover:bg-primary-100"
                title={`切换到后台会话（${backgroundSessions.length} 个进行中）`}
              >
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary-500 opacity-75" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary-600" />
                </span>
                后台进行中
              </button>
            )}

            {/* C3：转人工。坐席没接入前可撤回；已接入则只显示状态，不再重复建单 */}
            {user && (
              <button
                onClick={() => {
                  if (!handoff.active) void handoff.askHuman(lastAsk);
                }}
                disabled={handoff.active || handoff.busy}
                title={
                  handoff.active
                    ? '这张单还在处理中，坐席的回复会直接出现在会话里'
                    : '让真人坐席接手这个会话'
                }
                className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:border-slate-100 disabled:bg-slate-50 disabled:text-slate-400"
              >
                <Headset className="h-3.5 w-3.5" />
                {handoff.active ? '人工处理中' : handoff.busy ? '转接中…' : '转人工'}
              </button>
            )}

            {/* 新会话 + 未登录提示 */}
            <div className="relative">
              <button
                onClick={handleNewSession}
                className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 hover:border-slate-300 hover:bg-slate-50"
              >
                <Plus className="h-3.5 w-3.5" /> 新会话
              </button>
              {loginHint && (
                <div className="absolute right-0 top-full z-10 mt-1.5 whitespace-nowrap rounded-lg bg-slate-800 px-3 py-1.5 text-xs text-white shadow-lg">
                  请先登录后再开启新会话
                  <div className="absolute -top-1 right-4 h-2 w-2 rotate-45 bg-slate-800" />
                </div>
              )}
            </div>

            {/* 登录按钮 / 用户菜单 */}
            {user ? (
              <div className="relative">
                <button
                  onClick={() => setUserMenuOpen((v) => !v)}
                  className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white py-1 pl-1 pr-2.5 hover:border-slate-300"
                >
                  <span className="flex h-6 w-6 items-center justify-center rounded-md bg-primary-700 text-xs font-medium text-white">
                    {user.name.slice(0, 1)}
                  </span>
                  <span className="max-w-[72px] truncate text-xs text-slate-700">{user.name}</span>
                </button>
                {userMenuOpen && (
                  <>
                    <div className="fixed inset-0 z-10" onClick={() => setUserMenuOpen(false)} />
                    <div className="absolute right-0 top-full z-20 mt-1.5 w-40 rounded-lg border border-slate-200 bg-white py-1 shadow-lg">
                      <div className="border-b border-slate-100 px-3 py-2 text-xs text-slate-400">
                        {user.phone}
                      </div>
                      <button
                        onClick={() => {
                          setUserMenuOpen(false);
                          void logout();
                        }}
                        className="w-full px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-50"
                      >
                        退出登录
                      </button>
                    </div>
                  </>
                )}
              </div>
            ) : (
              <button
                onClick={() => navigate('/login')}
                className="rounded-lg bg-primary-700 px-3.5 py-1.5 text-xs font-medium text-white hover:bg-primary-800"
              >
                登录
              </button>
            )}
          </div>
        </header>

        {/* 消息区 */}
        <div className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-2xl xl:max-w-3xl">
            {allMessages.length === 0 && (
              <div className="flex h-full min-h-[320px] flex-col items-center justify-center gap-2 px-8 text-center">
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary-700">
                  <svg viewBox="0 0 24 24" className="h-6 w-6 text-white" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M3 12h4l2-6 4 12 2-6h6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </div>
                <div className="mt-2 text-base font-semibold text-slate-800">您好，我是您的问诊助手</div>
                <p className="max-w-xs text-sm leading-relaxed text-slate-500">
                  打字或用语音说出症状，我会帮您分析可能的疾病、推荐就诊科室，也可以直接帮您完成挂号。
                </p>
                {!user && (
                  <button
                    onClick={() => navigate('/login')}
                    className="mt-2 rounded-lg border border-primary-200 bg-primary-50 px-4 py-2 text-xs font-medium text-primary-700 hover:bg-primary-100"
                  >
                    登录后可同步问诊记录与预约
                  </button>
                )}
              </div>
            )}
            <MessageList messages={allMessages} />

            {hasError && !isStreaming && (
              <div className="flex justify-center pb-2">
                <button
                  onClick={() => retryLast()}
                  className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-4 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
                >
                  <RefreshCw className="h-3.5 w-3.5" /> 重试
                </button>
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        </div>

        {/* 输入区 */}
        <div className="border-t border-slate-200 bg-white">
          <div className="mx-auto w-full max-w-2xl xl:max-w-3xl">
            {handoff.ticket && (
              <HandoffBar
                ticket={handoff.ticket}
                busy={handoff.busy}
                error={handoff.error}
                onCancel={() => void handoff.cancel()}
                onDismiss={handoff.dismiss}
              />
            )}
            {!handoff.ticket && handoff.error && (
              <div className="px-4 pb-1 pt-2 text-[11px] text-red-600">{handoff.error}</div>
            )}
            <ChatInput
              value={input}
              onChange={setInput}
              onSend={handleSend}
              onStop={stop}
              isStreaming={isStreaming}
              voiceState={voiceState}
              interim={interim}
              onToggleVoice={toggle}
              voiceError={voiceError}
              image={pendingImage}
              onPickImage={(f) => void handlePickImage(f)}
              onClearImage={() => setPendingImage(null)}
              imageError={imageError}
            />
          </div>
        </div>
      </div>

      {/* 澄清确认 Modal */}
      {clarifyQuestion && (
        <ClarifyModal
          question={clarifyQuestion}
          onConfirm={(answer) => {
            dismissClarify();
            setInput(answer);
          }}
          onClose={dismissClarify}
        />
      )}

      {/* 预约成功后的引导（游客无"我的预约"，不展示入口） */}
      {user && handoffCards.length > 0 && !isStreaming && (
        <div className="fixed bottom-24 left-1/2 z-10 -translate-x-1/2 md:bottom-28">
          <button
            onClick={() => navigate('/appointments')}
            className="rounded-lg border border-primary-100 bg-white px-4 py-2 text-xs font-medium text-primary-700 shadow-lg hover:bg-primary-50"
          >
            查看我的预约
          </button>
        </div>
      )}
    </div>
  );
}
