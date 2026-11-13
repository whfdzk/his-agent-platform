import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import { AlertCircle, ChevronRight, FileText, Loader2, Upload } from 'lucide-react';
import { REPORT_STATUS_STYLES, REPORT_TYPE_OPTIONS, reportApi } from '@his/shared';
import type { HealthReport } from '@his/shared';

import { Button, Card, EmptyState, SectionHeader, Skeleton, Tag } from '../components/ui';

const ACCEPT = '.pdf,.jpg,.jpeg,.png,.webp';

export function ReportsPage() {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [reports, setReports] = useState<HealthReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [reportType, setReportType] = useState('lab');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await reportApi.list(50);
      setReports(res.reports);
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleFile = async (file?: File | null) => {
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) {
      setError('文件不能超过 20MB');
      return;
    }
    setUploading(true);
    setError('');
    try {
      const res = await reportApi.upload(file, reportType, true);
      await load();
      if (res.report.status === 'parsed') {
        navigate(`/reports/${res.report.id}`);
      }
    } catch (e) {
      setError((e as Error).message || '上传失败，请重试');
    } finally {
      setUploading(false);
      if (inputRef.current) inputRef.current.value = '';
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-5 md:pb-10 2xl:max-w-5xl">
        <header className="mb-4">
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">报告中心</h1>
          <p className="mt-0.5 text-xs text-slate-400">上传检查报告，自动提取指标并标记异常</p>
        </header>

        {/* 上传区 */}
        <Card className="mb-5 overflow-hidden p-0">
          <div
            className="flex cursor-pointer flex-col items-center gap-2 border-2 border-dashed border-slate-200 px-4 py-7 transition-colors hover:border-primary-300 hover:bg-primary-50/30"
            onClick={() => !uploading && inputRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              void handleFile(e.dataTransfer.files?.[0]);
            }}
          >
            <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-primary-50 text-primary-700">
              {uploading ? <Loader2 className="h-5 w-5 animate-spin" /> : <Upload className="h-5 w-5" />}
            </span>
            <div className="text-sm font-medium text-slate-700">
              {uploading ? '正在上传并解析，请稍候…' : '点击或拖拽上传报告'}
            </div>
            <div className="text-[11px] text-slate-400">支持 PDF / JPG / PNG / WEBP，单份不超过 20MB</div>
            {uploading && (
              <div className="mt-1 text-[10px] text-slate-400">扫描件 OCR 可能需要 30–60 秒</div>
            )}
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-slate-100 px-4 py-2.5">
            <div className="flex gap-1.5">
              {REPORT_TYPE_OPTIONS.map((o) => (
                <button
                  key={o.value}
                  onClick={() => setReportType(o.value)}
                  className={`rounded-full px-2.5 py-1 text-[11px] transition-colors ${
                    reportType === o.value
                      ? 'bg-primary-700 text-white'
                      : 'bg-slate-100 text-slate-500 hover:bg-slate-200'
                  }`}
                >
                  {o.label}
                </button>
              ))}
            </div>
            <Button size="sm" variant="secondary" onClick={() => inputRef.current?.click()} disabled={uploading}>
              选择文件
            </Button>
          </div>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT}
            className="hidden"
            onChange={(e) => void handleFile(e.target.files?.[0])}
          />
        </Card>

        {error && (
          <div className="mb-3 flex items-start gap-2 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            {error}
          </div>
        )}

        <SectionHeader title="我的报告" subtitle={`共 ${reports.length} 份`} />

        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
          </div>
        ) : reports.length === 0 ? (
          <Card>
            <EmptyState
              icon={<FileText className="h-5 w-5" />}
              title="还没有报告"
              description="上传体检或检验报告，AI 会帮你整理指标并提示异常项"
            />
          </Card>
        ) : (
          <div className="space-y-2">
            {reports.map((r) => {
              const status = REPORT_STATUS_STYLES[r.status] || REPORT_STATUS_STYLES.uploaded;
              const typeLabel = REPORT_TYPE_OPTIONS.find((o) => o.value === r.report_type)?.label;
              return (
                <Link
                  key={r.id}
                  to={`/reports/${r.id}`}
                  className="flex items-center gap-3 rounded-2xl border border-slate-100 bg-white px-4 py-3.5 shadow-card transition-colors hover:border-primary-200"
                >
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-50 text-violet-600">
                    <FileText className="h-5 w-5" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-slate-800">{r.file_name}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
                      <span>{typeLabel}</span>
                      <span>·</span>
                      <span>{r.hospital || '未知机构'}</span>
                      <span>·</span>
                      <span>{dayjs(r.report_date || r.created_at || undefined).format('YYYY-MM-DD')}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-1.5">
                      <Tag tone={r.status === 'parsed' ? 'emerald' : r.status === 'failed' ? 'red' : 'slate'}>
                        {status.label}
                      </Tag>
                      {r.status === 'parsed' && (
                        <>
                          <Tag tone="slate">{r.metric_count} 项指标</Tag>
                          {r.abnormal_count > 0 ? (
                            <Tag tone="amber">{r.abnormal_count} 项异常</Tag>
                          ) : (
                            <Tag tone="emerald">未见异常</Tag>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                  <ChevronRight className="h-4 w-4 shrink-0 text-slate-300" />
                </Link>
              );
            })}
          </div>
        )}

        <p className="mt-6 text-center text-[10px] leading-relaxed text-slate-400">
          报告原文仅用于本次解析，加密存储且不会用于模型训练。AI 解读不能替代医生诊断。
        </p>
      </div>
    </div>
  );
}
