import type { HandoffStreamEvent } from '../types';

export interface StreamHandoffOptions {
  /** patient：本人工单；seat：坐席台队列变更 */
  scope: 'patient' | 'seat';
  token?: string;
  signal?: AbortSignal;
  /** 每帧回调；断线后由调用方重连（这里只负责把这一条连接读完） */
  onEvent: (event: HandoffStreamEvent) => void;
  /** 首帧前（连接建立）回调，用来标记"推送在线" */
  onOpen?: () => void;
  /** 连接结束/失败回调，用来降级到轮询 */
  onClose?: (reason?: string) => void;
}

/**
 * 消费工单推送流（GET /api/v1/handoff/stream）。
 *
 * 为什么不用 EventSource：它不能自定义 Authorization 头，只能把 token 拼进
 * URL，凭证会留在代理日志与浏览器历史里。这里和聊天流一样用 fetch 读
 * ReadableStream，按 `data: {...}\n\n` 分帧；`: keepalive` 注释帧自动跳过。
 */
export async function streamHandoff(opts: StreamHandoffOptions): Promise<void> {
  const params = new URLSearchParams({ scope: opts.scope });
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;

  let resp: Response;
  try {
    resp = await fetch(`/api/v1/handoff/stream?${params.toString()}`, {
      method: 'GET',
      headers,
      signal: opts.signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    opts.onClose?.((e as Error).message);
    return;
  }

  if (!resp.ok || !resp.body) {
    opts.onClose?.(`HTTP ${resp.status}`);
    return;
  }

  opts.onOpen?.();

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const frames = buffer.split('\n\n');
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        const dataLine = frame.split('\n').find((line) => line.startsWith('data:'));
        if (!dataLine) continue; // keepalive / 注释帧
        try {
          opts.onEvent(JSON.parse(dataLine.slice(5).trim()) as HandoffStreamEvent);
        } catch {
          // 半帧或脏帧：跳过，不让它中断整条流
        }
      }
    }
    opts.onClose?.('stream ended');
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e;
    opts.onClose?.((e as Error).message);
  }
}
