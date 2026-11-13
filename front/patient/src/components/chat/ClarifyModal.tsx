import { motion, AnimatePresence } from 'framer-motion';
import { HelpCircle, X } from 'lucide-react';

/**
 * 问诊澄清确认 Modal：
 * 后端 SSE 发出 {"type":"clarify","question":"..."} 时弹出，
 * 用户可用文字补充说明（多轮对话），也可关闭后继续打字。
 */
export function ClarifyModal({
  question,
  onConfirm,
  onClose,
}: {
  question: string;
  onConfirm: (answer: string) => void;
  onClose: () => void;
}) {
  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.92, y: 16 }}
          animate={{ scale: 1, y: 0 }}
          exit={{ scale: 0.92, y: 16 }}
          className="w-full max-w-sm rounded-2xl bg-white p-5 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-3 flex items-center justify-between">
            <div className="flex items-center gap-2 text-base font-medium text-slate-900">
              <HelpCircle className="h-5 w-5 text-primary-700" />
              需要您补充一些信息
            </div>
            <button onClick={onClose} className="text-slate-400 hover:text-slate-600" aria-label="关闭">
              <X className="h-5 w-5" />
            </button>
          </div>

          <p className="mb-4 whitespace-pre-wrap text-sm leading-relaxed text-slate-600">{question}</p>

          <div className="flex gap-2">
            <button
              onClick={() => onConfirm('我明白了，让我补充描述')}
              className="flex-1 rounded-lg bg-primary-700 py-2.5 text-sm font-medium text-white hover:bg-primary-800"
            >
              好的，我来补充
            </button>
            <button
              onClick={onClose}
              className="rounded-lg border border-slate-200 px-4 py-2.5 text-sm text-slate-600 hover:bg-slate-50"
            >
              稍后再说
            </button>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
