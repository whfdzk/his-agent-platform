import { useCallback, useEffect, useRef, useState } from 'react';

// ── Web Speech API 最小类型声明（TS lib 未内置） ─────────────────────────
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}
interface SpeechRecognitionEventLike extends Event {
  resultIndex: number;
  results: { length: number; [i: number]: SpeechRecognitionResultLike };
}
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  abort(): void;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onerror: ((e: Event & { error?: string }) => void) | null;
  onend: (() => void) | null;
}

type VoiceState = 'idle' | 'recording' | 'transcribing' | 'error';

/**
 * 语音输入 hook（状态机：idle → recording → transcribing → idle）。
 * 首选浏览器 Web Speech API（Chrome/Edge，lang=zh-CN，实时转写上屏）；
 * 同时用 MediaRecorder 录音缓存（Blob），为将来接入后端 ASR 留输入，
 * 本期仅缓存不发送——Web Speech 不可用时提示用户改用键盘输入。
 */
export function useVoiceInput(onText: (text: string) => void) {
  const [state, setState] = useState<VoiceState>('idle');
  const [interim, setInterim] = useState('');
  const [errorMsg, setErrorMsg] = useState('');

  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const finalTextRef = useRef('');
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // onend 兜底定时器：组件卸载后仍可能触发 setState，必须单独记录并清理
  const fallbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      // 卸载时清理
      recognitionRef.current?.abort();
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
      if (timerRef.current) clearTimeout(timerRef.current);
      if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current);
    };
  }, []);

  const start = useCallback(() => {
    setErrorMsg('');
    finalTextRef.current = '';
    setInterim('');
    chunksRef.current = [];

    const w = window as unknown as {
      SpeechRecognition?: new () => SpeechRecognitionLike;
      webkitSpeechRecognition?: new () => SpeechRecognitionLike;
    };
    const SR = w.SpeechRecognition || w.webkitSpeechRecognition;

    // MediaRecorder 兜底录音（并行缓存）
    try {
      navigator.mediaDevices?.getUserMedia({ audio: true }).then((stream) => {
        const rec = new MediaRecorder(stream);
        rec.ondataavailable = (e) => chunksRef.current.push(e.data);
        rec.start();
        recorderRef.current = rec;
      }).catch(() => undefined);
    } catch {
      /* 忽略：无麦克风权限时 Web Speech 也会报错 */
    }

    if (SR) {
      const rec = new SR();
      rec.lang = 'zh-CN';
      rec.continuous = false;
      rec.interimResults = true;
      rec.onresult = (e) => {
        let interimText = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i];
          if (r.isFinal) finalTextRef.current += r[0].transcript;
          else interimText += r[0].transcript;
        }
        setInterim(interimText);
      };
      rec.onerror = (e) => {
        setErrorMsg(e.error === 'not-allowed' ? '麦克风权限被拒绝，请检查浏览器设置' : '语音识别出错');
      };
      rec.onend = () => {
        recorderRef.current?.state === 'recording' && recorderRef.current.stop();
        if (finalTextRef.current.trim()) {
          onText(finalTextRef.current.trim());
        } else if (interim) {
          // 只有中间结果时也采用
          onText(interim.trim());
        } else if (!errorMsg) {
          setErrorMsg('未识别到内容，请重试或改用键盘输入');
        }
        setState('idle');
        setInterim('');
      };
      recognitionRef.current = rec;
      try {
        rec.start();
        setState('recording');
        // 最长 60s 自动停止
        timerRef.current = setTimeout(() => rec.stop(), 60_000);
        return;
      } catch {
        /* fallthrough 到不支持提示 */
      }
    }

    // Web Speech 不可用：仅录音缓存，提示手动输入
    if (!recorderRef.current) {
      setErrorMsg('当前浏览器不支持语音识别（建议 Chrome/Edge），请直接输入文字');
      setState('idle');
      return;
    }
    setState('recording');
    timerRef.current = setTimeout(() => stop(), 60_000);
  }, [onText, errorMsg, interim]);

  const stop = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setState('transcribing');
    const rec = recognitionRef.current;
    if (rec) {
      rec.stop(); // onend 回调里提交文本
      if (fallbackTimerRef.current) clearTimeout(fallbackTimerRef.current);
      fallbackTimerRef.current = setTimeout(() => {
        // 兜底：部分浏览器 onend 不触发
        fallbackTimerRef.current = null;
        setState((s) => {
          if (s === 'transcribing') {
            if (finalTextRef.current.trim()) onText(finalTextRef.current.trim());
            return 'idle';
          }
          return s;
        });
      }, 1500);
    } else {
      // 仅 MediaRecorder：音频留作后续 ASR，本期直接结束
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
      setErrorMsg('当前浏览器不支持语音转文字，请改用键盘输入');
      setState('idle');
    }
  }, [onText]);

  const toggle = useCallback(() => {
    if (state === 'recording') stop();
    else if (state !== 'transcribing') start();
  }, [state, start, stop]);

  return { voiceState: state, interim, errorMsg, toggle, clearError: () => setErrorMsg('') };
}
