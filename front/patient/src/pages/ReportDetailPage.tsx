import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import dayjs from 'dayjs';
import {
  ArrowLeft,
  BarChart3,
  MessageCircle,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { REPORT_TYPE_OPTIONS, reportApi } from '@his/shared';
import type { ReportDetail } from '@his/shared';

import { Button, Card, EmptyState, SectionHeader, Skeleton, Tag } from '../components/ui';

const ABNORMAL_STYLE: Record<string, { label: string; cls: string }> = {
  high: { label: '偏高', cls: 'text-amber-600 bg-amber-50' },
  low: { label: '偏低', cls: 'text-sky-600 bg-sky-50' },
  abnormal: { label: '异常', cls: 'text-red-600 bg-red-50' },
  normal: { label: '正常', cls: 'text-slate-400 bg-slate-50' },
};

export function ReportDetailPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const reportId = Number(id);
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!Number.isFinite(reportId)) return;
    setLoading(true);
    setError('');
    try {
      const res = await reportApi.detail(reportId);
      setReport(res.report);
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [reportId]);

  useEffect(() => {
    void load();
  }, [load]);

  const grouped = useMemo(() => {
    const map = new Map<string, ReportDetail['metrics']>();
    (report?.metrics || []).forEach((m) => {
      const list = map.get(m.category) || [];
      list.push(m);
      map.set(m.category, list);
    });
    return Array.from(map.entries());
  }, [report]);

  const compareTarget = useMemo(() => {
    if (!report) return null;
    return report.metrics.find((m) => m.abnormal !== 'normal') || report.metrics[0] || null;
  }, [report]);

  const reparse = async () => {
    setBusy(true);
    setError('');
    try {
      const res = await reportApi.reparse(reportId);
      setReport(res.report);
    } catch (e) {
      setError((e as Error).message || '解析失败');
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm('删除后报告与指标将不可恢复，确定删除？')) return;
    setBusy(true);
    try {
      await reportApi.remove(reportId);
      navigate('/reports', { replace: true });
    } catch (e) {
      setError((e as Error).message || '删除失败');
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-3xl space-y-3 px-4 pt-5 2xl:max-w-5xl">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-32" />
        <Skeleton className="h-48" />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-4 md:pb-10 2xl:max-w-5xl">
        <div className="mb-3 flex items-center justify-between">
          <button
            onClick={() => navigate(-1)}
            className="flex h-9 items-center gap-1 rounded-lg px-1 text-sm text-slate-500 hover:text-slate-700"
          >
            <ArrowLeft className="h-4 w-4" /> 返回
          </button>
          <div className="flex items-center gap-1.5">
            {compareTarget && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() =>
                  navigate(
                    `/reports/compare?code=${encodeURIComponent(compareTarget.code || '')}&name=${encodeURIComponent(
                      compareTarget.name,
                    )}`,
                  )
                }
              >
                <BarChart3 className="h-3.5 w-3.5" /> 指标对比
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={remove} disabled={busy}>
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>

        {error && <div className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{error}</div>}

        {!report ? (
          <Card>
            <EmptyState icon={<ShieldAlert className="h-5 w-5" />} title="报告不存在或已删除" />
          </Card>
        ) : (
          <>
            {/* 报告信息 */}
            <Card className="mb-4 p-4">
              <div className="flex items-start gap-3">
                <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-violet-50 text-violet-600">
                  <Sparkles className="h-5 w-5" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-semibold text-slate-900">{report.file_name}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-400">
                    <span>{REPORT_TYPE_OPTIONS.find((o) => o.value === report.report_type)?.label}</span>
                    <span>·</span>
                    <span>{report.hospital || '未知机构'}</span>
                    <span>·</span>
                    <span>{dayjs(report.report_date || report.created_at || undefined).format('YYYY-MM-DD')}</span>
                    {report.parse_engine && (
                      <>
                        <span>·</span>
                        <span>解析引擎 {report.parse_engine}</span>
                      </>
                    )}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    <Tag tone="slate">{report.metric_count} 项指标</Tag>
                    {report.abnormal_count > 0 ? (
                      <Tag tone="amber">{report.abnormal_count} 项异常</Tag>
                    ) : (
                      <Tag tone="emerald">均在参考区间</Tag>
                    )}
                    {report.status === 'failed' && <Tag tone="red">解析失败</Tag>}
                  </div>
                </div>
              </div>
              {report.status === 'failed' && (
                <div className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-[11px] leading-relaxed text-red-600">
                  解析失败：{report.error_message || '文件可能不清晰或格式不受支持'}
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" variant="secondary" onClick={reparse} disabled={busy}>
                      <RefreshCw className="h-3.5 w-3.5" /> 重新解析
                    </Button>
                  </div>
                </div>
              )}
            </Card>

            {/* AI 摘要 */}
            {report.summary && (
              <Card className="mb-4 overflow-hidden p-0">
                <div className="flex items-center gap-2 bg-gradient-to-r from-primary-700 to-primary-600 px-4 py-2.5 text-white">
                  <Sparkles className="h-4 w-4" />
                  <span className="text-xs font-medium">智能解读</span>
                </div>
                <p className="px-4 py-3 text-[13px] leading-relaxed text-slate-600">{report.summary}</p>
                <div className="flex items-center justify-between border-t border-slate-100 px-4 py-2.5">
                  <span className="text-[10px] text-slate-400">规则化解读，仅供参考</span>
                  <Link
                    to="/consult"
                    className="flex items-center gap-1 text-[11px] font-medium text-primary-700"
                  >
                    <MessageCircle className="h-3.5 w-3.5" /> 问 AI 详情
                  </Link>
                </div>
              </Card>
            )}

            {/* 指标明细 */}
            <SectionHeader title="指标明细" subtitle="按分类展示，异常项已高亮" />
            {grouped.length === 0 ? (
              <Card>
                <EmptyState
                  icon={<ShieldAlert className="h-5 w-5" />}
                  title="未识别到结构化指标"
                  description="可尝试重新解析，或手动在健康管理中录入关键指标"
                  action={
                    <Button size="sm" variant="secondary" onClick={reparse} disabled={busy}>
                      <RefreshCw className="h-3.5 w-3.5" /> 重新解析
                    </Button>
                  }
                />
              </Card>
            ) : (
              <div className="space-y-3">
                {grouped.map(([category, items]) => (
                  <Card key={category} className="overflow-hidden p-0">
                    <div className="border-b border-slate-100 bg-slate-50/60 px-4 py-2 text-[11px] font-medium text-slate-500">
                      {category}
                    </div>
                    <div className="divide-y divide-slate-50">
                      {items.map((m) => {
                        const s = ABNORMAL_STYLE[m.abnormal] || ABNORMAL_STYLE.normal;
                        return (
                          <div key={m.id} className="flex items-center gap-3 px-4 py-2.5">
                            <div className="min-w-0 flex-1">
                              <div className="truncate text-xs text-slate-700">{m.name}</div>
                              <div className="text-[10px] text-slate-400">
                                参考 {m.ref_text || `${m.ref_low ?? '-'}–${m.ref_high ?? '-'}`} {m.unit}
                              </div>
                            </div>
                            <div className="text-right">
                              <div className="font-numeric text-sm font-medium text-slate-800">
                                {m.value_text}
                                <span className="ml-0.5 text-[10px] font-normal text-slate-400">{m.unit}</span>
                              </div>
                            </div>
                            <span className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] ${s.cls}`}>
                              {s.label}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
