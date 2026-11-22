import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getStoredToken,
  patientHandoffApi,
  streamHandoff,
  type HandoffTicket,
} from '@his/shared';

import { appendAgentMessage } from './useChatStream';

const ACTIVE: HandoffTicket['status'][] = ['open', 'claimed'];
/** 已结束的工单状态条再留一会儿，让患者知道"人工环节结束了"，不用一直挂着 */
const KEEP_FINISHED_MS = 10 * 60 * 1000;

function parseLocalTime(v: string): number {
  // 后端 serialize 给的是 "YYYY-MM-DD HH:MM:SS"（本地时间，无时区）
  return v ? new Date(v.replace(' ', 'T')).getTime() : 0;
}

function isVisible(t: HandoffTicket | null): boolean {
  if (!t) return false;
  if (ACTIVE.includes(t.status)) return true;
  const end = parseLocalTime(t.resolved_at);
  return end > 0 && Date.now() - end < KEEP_FINISHED_MS;
}

/**
 * 患者侧工单状态：当前会话的在办工单 + 实时推送 + 轮询兜底。
 *
 * 三件事：
 * 1. 进会话时查一次「我的工单」，把本会话的在办单挂到状态条上；
 * 2. 订阅工单推送流：状态一变立刻更新（不用等刷新），坐席回复/收尾正文
 *    直接插进聊天记录；
 * 3. 推送断了就退回 15s 轮询 —— 只靠推送时，浏览器后台休眠会让人以为"没人理我"。
 */
export function usePatientHandoff(userId: string, sessionId: string) {
  const [ticket, setTicket] = useState<HandoffTicket | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [dismissed, setDismissed] = useState(false);

  const ticketRef = useRef<HandoffTicket | null>(null);
  ticketRef.current = ticket;
  const sidRef = useRef(sessionId);
  sidRef.current = sessionId;
  const uidRef = useRef(userId);
  uidRef.current = userId;

  const apply = useCallback((next: HandoffTicket | null) => {
    const prevNo = ticketRef.current?.ticket_no;
    if (next?.ticket_no !== prevNo) setDismissed(false); // 换了新单：重新展示
    setTicket(next);
  }, []);

  const refresh = useCallback(async () => {
    const sid = sidRef.current;
    const uid = uidRef.current;
    if (!uid || !sid) {
      apply(null);
      return;
    }
    try {
      // active_only=false：结单那一瞬间也要能拿到"已结束"的那条，状态条才能收尾；
      // 只看最近 10 条，历史工单不该铺满内存。
      const r = await patientHandoffApi.mine({ session_id: sid, active_only: false });
      const pick = r.items.find((i) => i.session_id === sid) ?? null;
      if (isVisible(pick)) apply(pick);
      else apply(ACTIVE.includes(ticketRef.current?.status as HandoffTicket['status']) ? ticketRef.current : null);
    } catch {
      /* 拉不到就保持现状，推送或下一次轮询会纠正 */
    }
  }, [apply]);

  useEffect(() => {
    setTicket(null);
    setDismissed(false);
    if (!userId || !sessionId) return;
    void refresh();
  }, [userId, sessionId, refresh]);

  // ── 推送（断线自动重连，失败退回轮询） ──
  useEffect(() => {
    if (!userId) return;
    let stopped = false;
    let ctrl: AbortController | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      if (stopped) return;
      ctrl = new AbortController();
      void streamHandoff({
        scope: 'patient',
        token: getStoredToken(),
        signal: ctrl.signal,
        onEvent: (ev) => {
          if (ev.type === 'snapshot') {
            const pick = ev.items.find((i) => i.session_id === sidRef.current) ?? null;
            apply(pick);
            return;
          }
          if (ev.type === 'ticket') {
            if (ev.ticket.session_id !== sidRef.current) return;
            apply(isVisible(ev.ticket) ? ev.ticket : null);
            return;
          }
          if (ev.type === 'message') {
            // 只认当前会话那张单的消息，别把别的会话的坐席回复插进来
            const cur = ticketRef.current;
            if (cur && ev.ticket_no && cur.ticket_no !== ev.ticket_no) return;
            appendAgentMessage(
              sidRef.current,
              ev.content,
              ev.event_type === 'handoff_close' ? 'handoff_close' : 'human_agent',
            );
          }
        },
        onClose: () => {
          if (stopped) return;
          retryTimer = setTimeout(connect, 3000);
        },
      });
    };

    connect();
    // 轮询兜底：推送在线时 30s 一次，断流时 15s 一次（refresh 本身很轻）
    const poll = window.setInterval(() => void refresh(), 30_000);

    return () => {
      stopped = true;
      ctrl?.abort();
      if (retryTimer) clearTimeout(retryTimer);
      clearInterval(poll);
    };
  }, [userId, refresh, apply]);

  /** 点「转人工」：建单（同会话已有在办单时后端会直接把那张返回，created=false） */
  const askHuman = useCallback(
    async (summary = '') => {
      if (!userId || !sessionId) return;
      setBusy(true);
      setError('');
      try {
        const r = await patientHandoffApi.create({
          session_id: sessionId,
          reason: 'patient_request',
          priority: 'normal',
          summary: String(summary || '').slice(0, 200),
        });
        apply(r.ticket);
      } catch (e) {
        setError((e as Error).message || '转人工失败，请稍后重试');
      } finally {
        setBusy(false);
      }
    },
    [userId, sessionId, apply],
  );

  /** 撤回：只有还没人接的单能撤 */
  const cancel = useCallback(async () => {
    const cur = ticketRef.current;
    if (!cur) return;
    setBusy(true);
    setError('');
    try {
      const r = await patientHandoffApi.cancel(cur.id);
      apply({ ...r.ticket, resolved_at: r.ticket.resolved_at || r.ticket.created_at });
    } catch (e) {
      setError((e as Error).message || '撤回失败');
    } finally {
      setBusy(false);
    }
  }, [apply]);

  const active = !!ticket && ACTIVE.includes(ticket.status);

  return {
    ticket: !isVisible(ticket) || dismissed ? null : ticket,
    active,
    busy,
    error,
    askHuman,
    cancel,
    dismiss: () => setDismissed(true),
    refresh,
  };
}
