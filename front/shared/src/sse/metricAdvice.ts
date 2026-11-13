import type { MetricAdviceEvent } from '../types';

export interface StreamAdviceOptions {
  /** 登录态 Bearer Token（患者端该接口必须登录） */
  token?: string;
  signal?: AbortSignal;
  onEvent: (event: MetricAdviceEvent) => void;
}

/**
 * 消费体征记录「风险解读」流式接口（POST /api/v1/patient/metrics/{id}/advice）。
 *
 * 为什么用 SSE 而不是普通请求：单次解读要经过知识库检索 + 一次 LLM 生成，
 * 实测 10-60s 波动，一次性返回会让用户以为页面卡死。
 */
export async function streamMetricAdvice(
  metricId: number,
  opts: StreamAdviceOptions,
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (opts.token) {
    headers.Authorization = `Bearer ${opts.token}`;
  }

  const resp = await fetch(`/api/v1/patient/metrics/${metricId}/advice`, {
    method: 'POST',
    headers,
    signal: opts.signal,
  });

  if (resp.status === 401) {
    throw Object.assign(new Error('登录已失效，请重新登录'), { name: 'UnauthorizedError' });
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`解读失败 HTTP ${resp.status}`);
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
      const dataLine = frame.split('\n').find((line) => line.startsWith('data:'));
      if (!dataLine) continue; // 心跳/注释帧
      try {
        opts.onEvent(JSON.parse(dataLine.slice(5).trim()) as MetricAdviceEvent);
      } catch {
        // 忽略无法解析的帧
      }
    }
  }
}
