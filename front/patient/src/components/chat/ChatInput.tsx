import { useEffect, useRef } from 'react';
import { Mic, Send, Square } from 'lucide-react';
import clsx from 'clsx';

interface ChatInputProps {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  isStreaming: boolean;
  voiceState: 'idle' | 'recording' | 'transcribing' | 'error';
  interim: string;
  onToggleVoice: () => void;
}

export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  isStreaming,
  voiceState,
  interim,
  onToggleVoice,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const recording = voiceState === 'recording';

  // 录音时把实时转写文本显示在输入框中
  const displayValue = recording ? value + interim : value;

  useEffect(() => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
    }
  }, [displayValue]);

  return (
    <div className="px-3 py-2.5">
      {voiceState === 'error' && (
        <div className="mb-1.5 text-xs text-amber-600">语音识别暂不可用，请直接输入文字</div>
      )}
      <div className="flex items-end gap-2">
        <button
          type="button"
          onClick={onToggleVoice}
          disabled={isStreaming}
          className={clsx(
            'flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border transition-colors',
            recording
              ? 'animate-pulse border-red-200 bg-red-500 text-white'
              : 'border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700',
          )}
          aria-label={recording ? '停止录音' : '语音输入'}
        >
          <Mic className="h-5 w-5" />
        </button>

        <textarea
          ref={textareaRef}
          value={displayValue}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !recording) {
              e.preventDefault();
              if (value.trim() && !isStreaming) onSend();
            }
          }}
          rows={1}
          placeholder={recording ? '正在聆听…请说出您的症状' : '请描述您的症状，如：最近头疼发烧'}
          className="max-h-[120px] flex-1 resize-none rounded-lg border border-slate-200 bg-white px-3.5 py-2.5 text-sm text-slate-800 outline-none placeholder:text-slate-400 focus:border-teal-600 focus:ring-2 focus:ring-teal-600/10"
        />

        {isStreaming ? (
          <button
            type="button"
            onClick={onStop}
            className="flex h-10 shrink-0 items-center gap-1 rounded-lg bg-slate-800 px-4 text-sm text-white hover:bg-slate-700"
          >
            <Square className="h-3.5 w-3.5 fill-current" /> 停止
          </button>
        ) : (
          <button
            type="button"
            onClick={onSend}
            disabled={!value.trim() || recording}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-primary-700 text-white transition-colors hover:bg-primary-800 disabled:opacity-40"
            aria-label="发送"
          >
            <Send className="h-4 w-4" />
          </button>
        )}
      </div>
    </div>
  );
}
