import { useCallback, useRef, useSyncExternalStore } from 'react';
import { sessionApi, streamChat, type AppointmentData, type ChatMessage } from '@his/shared';

let msgSeq = 0;
function nextId() {
  return `m${Date.now()}-${msgSeq++}`;
}

/** 生成客户端幂等键：重试复用同一 key，服务端 SETNX 去重 */
function newClientMessageId() {
  return `cm-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

/** 登录态 token（store/session.tsx 持久化于 localStorage） */
function getToken(): string | undefined {
  return localStorage.getItem('his_token') || undefined;
}

export interface ChatState {
  messages: ChatMessage[];
  handoffCards: ChatMessage[];
  isStreaming: boolean;
  clarifyQuestion: string | null;
}

const emptyState = (): ChatState => ({
  messages: [],
  handoffCards: [],
  isStreaming: false,
  clarifyQuestion: null,
});

/**
 * 模块级会话缓存：切换/新开会话后，未完成的 SSE 流继续接收并写入对应会话，
 * 切回时状态完整恢复（"任务后台继续"）。
 * 组件通过 useSyncExternalStore 订阅，缓存变更必然触发重渲染。
 */
const cache = new Map<string, ChatState>();
const aborts = new Map<string, AbortController>();

// 本地会话缓存上限：长期使用（尤其游客频繁新开会话）不能让 Map 无限增长
const MAX_CACHED_SESSIONS = 30;

const listeners = new Set<() => void>();
let version = 0;
function notify() {
  version += 1;
  listeners.forEach((l) => l());
}
function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

function getCache(sid: string): ChatState {
  if (!cache.has(sid)) cache.set(sid, emptyState());
  return cache.get(sid)!;
}

function updateSession(sid: string, up: (s: ChatState) => ChatState) {
  if (!cache.has(sid) && cache.size >= MAX_CACHED_SESSIONS) {
    // 按插入顺序淘汰最旧的一条；正在流式接收的会话不能淘汰，否则切回会丢状态
    for (const key of cache.keys()) {
      if (key !== sid && !aborts.has(key)) {
        cache.delete(key);
        break;
      }
    }
  }
  cache.set(sid, up(getCache(sid)));
  notify();
}

/** 会话是否已有本地内容（判断是否需要从后端拉历史） */
export function hasLocalMessages(sid: string): boolean {
  const s = cache.get(sid);
  return !!s && (s.messages.length > 0 || s.isStreaming);
}

/** 用后端历史消息填充会话缓存（只在本地为空时调用） */
export function seedSessionMessages(sid: string, msgs: { role: string; content: string }[]) {
  if (hasLocalMessages(sid)) return;
  const messages: ChatMessage[] = msgs
    .filter((m) => m.role === 'user' || m.role === 'assistant')
    .map((m) => ({
      id: nextId(),
      role: m.role as 'user' | 'assistant',
      content: m.content,
    }));
  updateSession(sid, (s) => ({ ...s, messages }));
}

/** 正在后台流式运行的其它会话列表（用于顶部徽标） */
export function useBackgroundSessions(currentSid: string): string[] {
  useSyncExternalStore(subscribe, () => version);
  const out: string[] = [];
  cache.forEach((s, sid) => {
    if (sid !== currentSid && s.isStreaming) out.push(sid);
  });
  return out;
}

/**
 * 聊天流式 hook（多会话版）：
 * - SSE 流式渲染（token 事件追加，done 固化）
 * - 每个会话独立 AbortController；只有用户点"停止"或重试才中断，
 *   切换/新开会话不中断，旧会话在后台继续
 * - clarify / appointment 结构化事件驱动 Modal 与挂号卡片
 * - 错误消息支持重试
 */
export function useChatStream(userId: string, sessionId: string) {
  const sessionRef = useRef(sessionId);
  sessionRef.current = sessionId;

  // 直接订阅缓存：任何会话更新都会重新取快照，当前会话变更即重渲染
  const state = useSyncExternalStore(subscribe, () => getCache(sessionId));

  const sendMessage = useCallback(
    async (text: string, onAppointment?: (a: AppointmentData) => void) => {
      const sid = sessionRef.current;
      if (getCache(sid).isStreaming) return;
      const trimmed = text.trim();
      if (!trimmed) return;

      const userMsg: ChatMessage = { id: nextId(), role: 'user', content: trimmed };
      const botMsg: ChatMessage = { id: nextId(), role: 'assistant', content: '', streaming: true };
      updateSession(sid, (s) => ({
        ...s,
        messages: [...s.messages, userMsg, botMsg],
        isStreaming: true,
      }));

      const controller = new AbortController();
      aborts.set(sid, controller);

      // 幂等键：同一条消息的所有重试（含网络异常退避重连）复用
      const clientMessageId = newClientMessageId();

      const appendBot = (chunk: string) => {
        updateSession(sid, (s) => ({
          ...s,
          messages: s.messages.map((m) =>
            m.id === botMsg.id ? { ...m, content: m.content + chunk } : m,
          ),
        }));
      };

      const markError = (content: string) => {
        updateSession(sid, (s) => ({
          ...s,
          messages: s.messages.map((m) =>
            m.id === botMsg.id ? { ...m, role: 'error', content } : m,
          ),
        }));
      };

      try {
        // 网络异常时指数退避重试（1s/2s/4s，最多 3 次）：
        // 幂等键不变，若服务端已完成会直接重放结果，不会重复计费/重复回复
        const maxAttempts = 3;
        let lastError: Error | null = null;

        // 断线续传：重试仍失败时，从服务端拉取已落库但本次没收到的事件
        const resumeFromServer = async (): Promise<boolean> => {
          try {
            const { events } = await sessionApi.pendingEvents(sid, 0);
            // 只认"本次提问之后"的助手回复，否则会把上一轮答案当成这一轮的
            let askIdx = -1;
            for (let i = events.length - 1; i >= 0; i--) {
              if (events[i].role === 'user' && events[i].content === trimmed) {
                askIdx = i;
                break;
              }
            }
            if (askIdx < 0) return false;
            const latest = events
              .slice(askIdx + 1)
              .filter((e) => (e.role ?? 'assistant') === 'assistant')
              .pop();
            if (latest?.content) {
              updateSession(sid, (s) => ({
                ...s,
                messages: s.messages.map((m) =>
                  m.id === botMsg.id
                    ? { ...m, role: 'assistant', content: latest.content!, streaming: false }
                    : m,
                ),
              }));
              return true;
            }
          } catch {
            // 拉取不到就保留原有错误提示
          }
          return false;
        };

        for (let attempt = 1; ; attempt++) {
          try {
            await streamChat(
              { session_id: sid, message: trimmed, client_message_id: clientMessageId },
              {
                signal: controller.signal,
                token: getToken(),
                onEvent: (event) => {
                  switch (event.type) {
                    case 'token':
                      appendBot(event.content);
                      break;
                    case 'clarify':
                      updateSession(sid, (s) => ({ ...s, clarifyQuestion: event.question }));
                      break;
                    case 'appointment':
                      updateSession(sid, (s) => ({
                        ...s,
                        handoffCards: [
                          ...s.handoffCards,
                          {
                            id: `handoff-${Date.now()}-${msgSeq++}`,
                            role: 'handoff',
                            content: '',
                            appointment: event.data,
                          },
                        ],
                      }));
                      onAppointment?.(event.data);
                      break;
                    case 'safety':
                      // 输出审核：有 content 表示正文被纠正，需要整体替换
                      updateSession(sid, (s) => ({
                        ...s,
                        messages: s.messages.map((m) =>
                          m.id === botMsg.id
                            ? {
                                ...m,
                                content: event.content ?? m.content,
                                safetyNotice: event.notice || '该回复已通过安全审核并补充必要提示',
                                safetyLevel: event.level,
                              }
                            : m,
                        ),
                      }));
                      break;
                    case 'done':
                      break;
                    case 'error':
                      markError(event.message || '服务异常，请稍后重试');
                      break;
                  }
                },
              },
            );
            lastError = null;
            break;
          } catch (err) {
            const e = err as Error;
            if (e.name === 'AbortError') throw err;
            // 鉴权/限流类错误不重试，直接提示
            if (e.name === 'UnauthorizedError' || e.name === 'RateLimitError') {
              markError(e.message);
              lastError = null;
              break;
            }
            lastError = e;
            if (attempt >= maxAttempts) break;
            // 可中断的退避：点"停止"后不应还要等满 4s 才结束
            await new Promise<void>((resolve, reject) => {
              const onAbort = () => {
                clearTimeout(timer);
                reject(new DOMException('Aborted', 'AbortError'));
              };
              const timer = setTimeout(() => {
                controller.signal.removeEventListener('abort', onAbort);
                resolve();
              }, 1000 * 2 ** (attempt - 1));
              controller.signal.addEventListener('abort', onAbort, { once: true });
            });
          }
        }

        if (lastError && !(await resumeFromServer())) {
          markError(`网络异常：${lastError.message}`);
        }
      } catch (err) {
        if ((err as Error).name === 'AbortError') {
          // 用户主动停止：保留已收到内容，标记已停止
          updateSession(sid, (s) => ({
            ...s,
            messages: s.messages.map((m) =>
              m.id === botMsg.id
                ? { ...m, streaming: false, stopped: true, content: m.content || '（已停止生成）' }
                : m,
            ),
          }));
        } else {
          markError(`网络异常：${(err as Error).message}`);
        }
      } finally {
        updateSession(sid, (s) => ({
          ...s,
          isStreaming: false,
          messages: s.messages.map((m) => (m.id === botMsg.id ? { ...m, streaming: false } : m)),
        }));
        aborts.delete(sid);
      }
    },
    [userId],
  );

  /** 重试：移除最后一条错误消息与对应提问，重发 */
  const retryLast = useCallback(
    async (onAppointment?: (a: AppointmentData) => void) => {
      const sid = sessionRef.current;
      let question = '';
      updateSession(sid, (s) => {
        const msgs = [...s.messages];
        while (msgs.length) {
          const last = msgs[msgs.length - 1];
          if (last.role === 'error') {
            msgs.pop();
            continue;
          }
          if (last.role === 'user') {
            question = last.content;
            msgs.pop();
            break;
          }
          break;
        }
        return { ...s, messages: msgs };
      });
      if (question) await sendMessage(question, onAppointment);
    },
    [sendMessage],
  );

  /** 停止当前会话的生成（仅显式调用；切换会话不会中断后台流） */
  const stop = useCallback(() => {
    aborts.get(sessionRef.current)?.abort();
  }, []);

  const dismissClarify = useCallback(() => {
    updateSession(sessionRef.current, (s) => ({ ...s, clarifyQuestion: null }));
  }, []);

  return {
    messages: state.messages,
    handoffCards: state.handoffCards,
    isStreaming: state.isStreaming,
    clarifyQuestion: state.clarifyQuestion,
    sendMessage,
    retryLast,
    stop,
    dismissClarify,
  };
}

/**
 * 登出 / 凭证失效时中断所有进行中的 SSE：
 * 后端检测到断连会取消对应的 LLM 任务，避免"人已经退了，还在后台烧 token"。
 */
function abortAllStreams(): void {
  aborts.forEach((controller) => controller.abort());
  aborts.clear();
}

if (typeof window !== 'undefined') {
  window.addEventListener('his:logout', abortAllStreams);
  window.addEventListener('his:unauthorized', abortAllStreams);
}
