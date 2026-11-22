import { useCallback, useEffect, useRef, useState } from 'react';

import { PcmRecorder } from '../lib/wavRecorder';
import { transcribeAudio } from '../lib/voiceApi';

type VoiceState = 'idle' | 'recording' | 'transcribing' | 'error';

/** 单次录音上限：超过自动停止并送识别，避免长录音把内存和上游配额拖垮 */
const MAX_RECORD_MS = 60_000;

/**
 * 语音输入 hook（状态机：idle → recording → transcribing → idle/error）。
 *
 * 走本地录音 + 后端 ASR：浏览器自带的 Web Speech 依赖厂商服务（Chrome 走 Google、
 * Edge 走 Azure），国内 Chrome 上基本不可用；换成后端转写后，任何支持 Web Audio
 * 的浏览器都能用，且识别效果与文本口径统一。
 */
export function useVoiceInput(onText: (text: string) => void) {
  const [state, setState] = useState<VoiceState>('idle');
  const [interim, setInterim] = useState('');
  const [errorMsg, setErrorMsg] = useState('');

  const recorderRef = useRef<PcmRecorder | null>(null);
  const autoStopRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 自动停止定时器要调用 stop，而 stop 定义在 start 之后，用 ref 避免引用顺序问题
  const stopRef = useRef<() => void>(() => undefined);
  const mountedRef = useRef(true);
  // onText 可能随渲染变化，用 ref 持有最新引用，避免 start/stop 依赖抖动
  const onTextRef = useRef(onText);
  onTextRef.current = onText;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (autoStopRef.current) clearTimeout(autoStopRef.current);
      recorderRef.current?.cancel();
      recorderRef.current = null;
    };
  }, []);

  const safeSetState = useCallback((next: VoiceState) => {
    if (mountedRef.current) setState(next);
  }, []);

  const start = useCallback(async () => {
    if (recorderRef.current?.active) return;
    setErrorMsg('');
    setInterim('');

    const recorder = new PcmRecorder();
    try {
      await recorder.start();
    } catch (e) {
      recorder.cancel();
      const denied = e instanceof Error && /denied|NotAllowed/i.test(e.message);
      setErrorMsg(denied ? '麦克风权限被拒绝，请在浏览器设置里允许后重试' : '无法开始录音，请检查麦克风');
      safeSetState('error');
      return;
    }
    if (!mountedRef.current) {
      recorder.cancel();
      return;
    }
    recorderRef.current = recorder;
    safeSetState('recording');
    autoStopRef.current = setTimeout(() => stopRef.current(), MAX_RECORD_MS);
  }, [safeSetState]);

  const stop = useCallback(async () => {
    if (autoStopRef.current) {
      clearTimeout(autoStopRef.current);
      autoStopRef.current = null;
    }
    const recorder = recorderRef.current;
    if (!recorder?.active) return;
    recorderRef.current = null;

    const blob = recorder.stop();
    if (!blob) {
      setErrorMsg('录音太短，请按住多说几句');
      safeSetState('error');
      return;
    }

    safeSetState('transcribing');
    try {
      const text = await transcribeAudio(blob);
      if (mountedRef.current) {
        onTextRef.current(text);
        safeSetState('idle');
      }
    } catch (e) {
      if (mountedRef.current) {
        setErrorMsg(e instanceof Error ? e.message : '语音识别失败，请改用键盘输入');
        safeSetState('error');
      }
    }
  }, [safeSetState]);
  stopRef.current = () => void stop();

  const toggle = useCallback(() => {
    if (state === 'recording') void stop();
    else if (state !== 'transcribing') void start();
  }, [state, start, stop]);

  const clearError = useCallback(() => {
    setErrorMsg('');
    setState((s) => (s === 'error' ? 'idle' : s));
  }, []);

  return { voiceState: state, interim, errorMsg, toggle, clearError };
}
