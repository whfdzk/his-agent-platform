import type { ChatEvent, ChatRequest } from '../types';

export interface StreamChatOptions {
  signal?: AbortSignal;
  /**
   * 登录态 Bearer Token（可选）。
   * - 携带：后端识别患者身份，消息与会话落库，可查历史记录
   * - 不携带：游客模式，正常聊天但对话不保存（仅会话级短期记忆），Token 失效返回 401
   */
  token?: string;
  onEvent: (event: ChatEvent) => void;
}

/**
 * 消费后端 SSE 流式对话接口（POST /api/v1/chat/stream）。
 * fetch + ReadableStream 按帧解析 `id: <seq>\ndata: {...}\n\n`。
 * - 通过 AbortSignal 支持用户随时停止（后端检测断连后会取消 LLM 任务）
 * - 每帧携带递增 seq（id 行），可上报给重连恢复逻辑
 * - 心跳帧（`: keepalive`）无 data 行，自动跳过
 */
export async function streamChat(req: ChatRequest, opts: StreamChatOptions): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (opts.token) {
    headers.Authorization = `Bearer ${opts.token}`;
  }

  const resp = await fetch('/api/v1/chat/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify(req),
    signal: opts.signal,
  });

  if (resp.status === 401) {
    throw Object.assign(new Error('登录已失效，请重新登录'), { name: 'UnauthorizedError' });
  }
  if (resp.status === 429 || resp.status === 503) {
    let detail = resp.status === 429 ? '发送过于频繁' : '当前在线人数较多';
    try {
      const body = await resp.json();
      if (body?.detail) detail = body.detail;
    } catch { /* ignore */ }
    throw Object.assign(new Error(detail), { name: 'RateLimitError' });
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`连接失败 HTTP ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const frames = buffer.split('\n\n');
    buffer = frames.pop() ?? '';

    for (const frame of frames) {
      const lines = frame.split('\n');
      const dataLine = lines.find((line) => line.startsWith('data:'));
      if (!dataLine) continue; // 心跳/注释帧
      const idLine = lines.find((line) => line.startsWith('id:'));
      const seq = idLine ? Number(idLine.slice(3).trim()) : undefined;
      try {
        const event = JSON.parse(dataLine.slice(5).trim()) as ChatEvent;
        if (seq !== undefined && Number.isFinite(seq)) {
          (event as { seq?: number }).seq = seq;
        }
        opts.onEvent(event);
      } catch {
        // 忽略无法解析的帧
      }
    }
  }
}
