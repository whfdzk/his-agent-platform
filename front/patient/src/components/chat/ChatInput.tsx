import { useEffect, useRef } from 'react';
import { ImagePlus, Loader2, Mic, Send, Square, X } from 'lucide-react';
import clsx from 'clsx';

interface PendingImage {
  previewUrl: string;
  name: string;
}

interface ChatInputProps {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  isStreaming: boolean;
  voiceState: 'idle' | 'recording' | 'transcribing' | 'error';
  interim: string;
  onToggleVoice: () => void;
  /** 语音失败的具体原因（麦克风权限 / 上游不可用 / 录音太短），没有就用兜底文案 */
  voiceError?: string;
  /** 待发送的图片（已压缩），为空表示纯文字消息 */
  image: PendingImage | null;
  onPickImage: (file: File) => void;
  onClearImage: () => void;
  imageError?: string;
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
  voiceError,
  image,
  onPickImage,
  onClearImage,
  imageError,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const recording = voiceState === 'recording';
  const transcribing = voiceState === 'transcribing';

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
        <div className="mb-1.5 text-xs text-amber-600">{voiceError || '语音识别暂不可用，请直接输入文字'}</div>
      )}
      {imageError && <div className="mb-1.5 text-xs text-amber-600">{imageError}</div>}

      {/* 图片预览：发送前可移除 */}
      {image && (
        <div className="mb-2 flex items-center gap-2 rounded-lg border border-slate-200 bg-slate-50 p-2">
          <img
            src={image.previewUrl}
            alt="待发送图片"
            className="h-14 w-14 rounded-md object-cover"
          />
          <div className="min-w-0 flex-1">
            <div className="truncate text-xs font-medium text-slate-700">{image.name}</div>
            <div className="text-[11px] text-slate-400">将识别图片内容后回答，图片不会保存</div>
          </div>
          <button
            type="button"
            onClick={onClearImage}
            aria-label="移除图片"
            className="rounded-md p-1 text-slate-400 transition-colors hover:bg-slate-200 hover:text-slate-600"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      <div className="flex items-end gap-2">
        <button
          type="button"
          onClick={onToggleVoice}
          disabled={isStreaming || transcribing}
          className={clsx(
            'flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border transition-colors',
            recording
              ? 'animate-pulse border-red-200 bg-red-500 text-white'
              : 'border-slate-200 bg-white text-slate-500 hover:border-slate-300 hover:text-slate-700',
          )}
          aria-label={recording ? '停止录音' : '语音输入'}
          title={transcribing ? '正在识别语音…' : recording ? '点击结束录音' : '点击开始录音'}
        >
          {transcribing ? <Loader2 className="h-5 w-5 animate-spin" /> : <Mic className="h-5 w-5" />}
        </button>

        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          disabled={isStreaming || recording}
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-500 transition-colors hover:border-slate-300 hover:text-slate-700 disabled:opacity-40"
          aria-label="上传图片"
          title="上传检查报告 / 药盒 / 患处照片"
        >
          <ImagePlus className="h-5 w-5" />
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onPickImage(file);
            e.target.value = ''; // 允许连续选同一张
          }}
        />

        <textarea
          ref={textareaRef}
          value={displayValue}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !recording) {
              e.preventDefault();
              if ((value.trim() || image) && !isStreaming) onSend();
            }
          }}
          rows={1}
          placeholder={
            recording
              ? '正在聆听…请说出您的症状，说完点麦克风结束'
              : transcribing
                ? '正在识别语音…'
                : image
                  ? '可以补充一句想问的问题，如：这个药怎么吃'
                  : '请描述您的症状，如：最近头疼发烧'
          }
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
            disabled={(!value.trim() && !image) || recording || transcribing}
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
