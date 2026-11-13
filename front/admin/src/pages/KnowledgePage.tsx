import { useEffect, useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { BookOpen, FileUp, Trash2, X, Bell, ThumbsUp, ThumbsDown } from 'lucide-react';
import clsx from 'clsx';
import {
  knowledgeApi,
  DOC_TYPE_OPTIONS,
  type KnowledgeDoc,
  type KnowledgeFeedbackStats,
  type KnowledgeNotification,
} from '@his/shared';

export function KnowledgePage() {
  const [docs, setDocs] = useState<KnowledgeDoc[]>([]);
  const [keyword, setKeyword] = useState('');
  const [uploadOpen, setUploadOpen] = useState(false);
  const [notifications, setNotifications] = useState<KnowledgeNotification[]>([]);
  const [feedback, setFeedback] = useState<KnowledgeFeedbackStats | null>(null);
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    Promise.all([
      knowledgeApi.listDocs().catch(() => ({ documents: [], total: 0 })),
      knowledgeApi.notifications().catch(() => ({ notifications: [], total: 0 })),
      knowledgeApi.feedbackStats().catch(() => null),
    ]).then(([d, n, f]) => {
      setDocs(d.documents);
      setNotifications(n.notifications);
      setFeedback(f);
      setLoading(false);
    });
  };

  useEffect(load, []);

  const filtered = docs.filter(
    (d) =>
      !keyword ||
      d.doc_name.toLowerCase().includes(keyword.toLowerCase()) ||
      d.category.includes(keyword),
  );

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-medium">知识库管理</h1>
        <button
          onClick={() => setUploadOpen(true)}
          className="flex items-center gap-1.5 rounded-xl bg-primary-500 px-4 py-2 text-sm text-white hover:bg-primary-600"
        >
          <FileUp className="h-4 w-4" /> 上传文档
        </button>
      </div>

      {/* 反馈统计 + 通知 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="flex items-center gap-4 rounded-2xl border border-gray-100 bg-white p-4">
          <ThumbsUp className="h-5 w-5 text-emerald-500" />
          <div>
            <div className="text-xs text-gray-400">有用反馈</div>
            <div className="text-lg font-medium">{(feedback as { positive?: number })?.positive ?? '-'}</div>
          </div>
        </div>
        <div className="flex items-center gap-4 rounded-2xl border border-gray-100 bg-white p-4">
          <ThumbsDown className="h-5 w-5 text-red-400" />
          <div>
            <div className="text-xs text-gray-400">无用反馈</div>
            <div className="text-lg font-medium">{(feedback as { negative?: number })?.negative ?? '-'}</div>
          </div>
        </div>
        <div className="rounded-2xl border border-gray-100 bg-white p-4">
          <div className="mb-1 flex items-center gap-1.5 text-xs text-gray-400">
            <Bell className="h-3.5 w-3.5" /> 未读知识更新通知
          </div>
          {notifications.length === 0 ? (
            <div className="text-sm text-gray-400">暂无</div>
          ) : (
            <div className="max-h-20 space-y-1 overflow-y-auto">
              {notifications.slice(0, 5).map((n) => (
                <button
                  key={n.id}
                  onClick={async () => {
                    await knowledgeApi.markNotificationsRead([n.id]).catch(() => undefined);
                    setNotifications((prev) => prev.filter((x) => x.id !== n.id));
                  }}
                  className="block w-full truncate rounded-lg bg-amber-50 px-2 py-1 text-left text-xs text-amber-700 hover:bg-amber-100"
                >
                  {n.doc_name}（{n.action}）
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 文档列表 */}
      <div className="rounded-2xl border border-gray-100 bg-white">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-100 px-4 py-3 sm:px-5">
          <h2 className="text-sm font-medium">
            文档列表 <span className="ml-1 text-xs text-gray-400">{filtered.length} 个</span>
          </h2>
          <input
            value={keyword}
            onChange={(e) => setKeyword(e.target.value)}
            placeholder="搜索文档名 / 分类…"
            className="w-full rounded-xl bg-gray-50 px-3 py-1.5 text-sm outline-none focus:ring-2 focus:ring-primary-100 sm:w-56"
          />
        </div>

        {loading ? (
          <div className="p-10 text-center text-sm text-gray-400">加载中…</div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center gap-2 p-12 text-center">
            <BookOpen className="h-10 w-10 text-gray-300" />
            <div className="text-sm text-gray-400">暂无文档，点击右上角上传</div>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-400">
                <th className="px-5 py-2.5 font-normal">文档名</th>
                <th className="px-5 py-2.5 font-normal">类型</th>
                <th className="px-5 py-2.5 font-normal">分类</th>
                <th className="px-5 py-2.5 font-normal text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((d) => (
                <tr key={d.doc_name} className="border-b border-gray-50 hover:bg-gray-50/60">
                  <td className="max-w-xs truncate px-5 py-3">{d.doc_name}</td>
                  <td className="px-5 py-3">
                    <span className="rounded-full bg-primary-50 px-2 py-0.5 text-xs text-primary-600">
                      {DOC_TYPE_OPTIONS.find((o) => o.value === d.doc_type)?.label || d.doc_type}
                    </span>
                  </td>
                  <td className="px-5 py-3 text-gray-600">{d.category}</td>
                  <td className="px-5 py-3 text-right">
                    <button
                      onClick={async () => {
                        if (!confirm(`确定删除「${d.doc_name}」？该操作会同时删除向量索引。`)) return;
                        await knowledgeApi.deleteDoc(d.doc_name).catch((e) => alert(e.message));
                        load();
                      }}
                      className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-red-500 hover:bg-red-50"
                    >
                      <Trash2 className="h-3.5 w-3.5" /> 删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </div>

      <UploadDialog open={uploadOpen} onClose={() => { setUploadOpen(false); load(); }} />
    </div>
  );
}

function UploadDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [docType, setDocType] = useState('guideline');
  const [category, setCategory] = useState('通用');
  const [uploading, setUploading] = useState(false);
  const [result, setResult] = useState<string>('');
  const [error, setError] = useState('');

  if (!open) return null;

  const submit = async () => {
    if (!file) {
      setError('请先选择文件');
      return;
    }
    setUploading(true);
    setError('');
    setResult('');
    try {
      const r = await knowledgeApi.upload(file, docType, category);
      setResult(`${r.message}，切分 ${r.chunks} 块`);
      setFile(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  };

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-3 sm:p-6"
        onClick={onClose}
      >
        <motion.div
          initial={{ scale: 0.94, y: 12 }}
          animate={{ scale: 1, y: 0 }}
          className="w-full max-w-md rounded-2xl bg-white p-6 shadow-xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-base font-medium">上传知识文档</h3>
            <button onClick={onClose} aria-label="关闭">
              <X className="h-5 w-5 text-gray-400" />
            </button>
          </div>

          {/* 拖拽区 */}
          <label
            className={clsx(
              'mb-4 flex cursor-pointer flex-col items-center gap-2 rounded-2xl border-2 border-dashed p-6 text-center transition-colors',
              file ? 'border-emerald-200 bg-emerald-50' : 'border-gray-200 hover:border-primary-200 hover:bg-primary-50/40',
            )}
          >
            <FileUp className={clsx('h-8 w-8', file ? 'text-emerald-500' : 'text-gray-300')} />
            {file ? (
              <span className="text-sm text-gray-700">{file.name}</span>
            ) : (
              <span className="text-sm text-gray-400">点击选择或拖拽文件（PDF / Word / TXT / MD）</span>
            )}
            <input
              type="file"
              accept=".pdf,.docx,.doc,.txt,.md"
              className="hidden"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>

          <div className="mb-4 grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-xs text-gray-500">文档类型</label>
              <select
                value={docType}
                onChange={(e) => setDocType(e.target.value)}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              >
                {DOC_TYPE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs text-gray-500">分类</label>
              <input
                value={category}
                onChange={(e) => setCategory(e.target.value)}
                className="w-full rounded-xl border border-gray-200 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-primary-100"
              />
            </div>
          </div>

          <div className="mb-4 rounded-xl bg-gray-50 px-3 py-2 text-xs text-gray-400">
            切分方式：当前后端使用默认切分策略（上传接口暂未开放切分参数选择）
          </div>

          {error && <div className="mb-3 text-xs text-red-500">{error}</div>}
          {result && <div className="mb-3 text-xs text-emerald-600">{result}</div>}

          <button
            onClick={submit}
            disabled={uploading}
            className="w-full rounded-xl bg-primary-500 py-2.5 text-sm text-white hover:bg-primary-600 disabled:opacity-50"
          >
            {uploading ? '上传并解析中…' : '开始上传'}
          </button>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
