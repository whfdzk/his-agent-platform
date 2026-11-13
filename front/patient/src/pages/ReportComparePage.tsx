import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  ArrowDownRight,
  ArrowLeft,
  ArrowUpRight,
  BarChart3,
  Minus,
} from 'lucide-react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { reportApi } from '@his/shared';
import type { ReportCompare, ReportMetricItem } from '@his/shared';

import { Button, Card, EmptyState, SectionHeader, Skeleton, Tag, inputCls } from '../components/ui';

export function ReportComparePage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [options, setOptions] = useState<ReportMetricItem[]>([]);
  const [active, setActive] = useState<{ code?: string; name?: string }>({
    code: params.get('code') || undefined,
    name: params.get('name') || undefined,
  });
  const [data, setData] = useState<ReportCompare | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // 用最近一份已解析报告的指标作为下拉选项
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await reportApi.list(10);
        const parsed = list.reports.find((r) => r.status === 'parsed');
        if (!parsed) return;
        const detail = await reportApi.detail(parsed.id);
        if (cancelled) return;
        setOptions(detail.report.metrics);
        if (!params.get('code') && !params.get('name')) {
          const first = detail.report.metrics.find((m) => m.abnormal !== 'normal') || detail.report.metrics[0];
          if (first) setActive({ code: first.code || undefined, name: first.name });
        }
      } catch {
        /* 选项加载失败不阻断 */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [params]);

  const load = useCallback(async () => {
    if (!active.code && !active.name) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    try {
      setData(await reportApi.compare({ code: active.code, name: active.name, limit: 24 }));
    } catch (e) {
      setError((e as Error).message || '对比数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [active]);

  useEffect(() => {
    void load();
  }, [load]);

  const chartData = (data?.points || []).map((p) => ({
    label: p.report_date || '',
    value: p.value_num,
    abnormal: p.abnormal,
  }));

  const DirectionIcon =
    data?.direction === 'up' ? ArrowUpRight : data?.direction === 'down' ? ArrowDownRight : Minus;

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
          <Tag tone="violet">跨报告对比</Tag>
        </div>

        <header className="mb-4">
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">指标对比</h1>
          <p className="mt-0.5 text-xs text-slate-400">同一指标的历史变化，帮助观察治疗效果</p>
        </header>

        {options.length > 0 && (
          <div className="mb-4">
            <label className="label-base">选择指标</label>
            <select
              className={inputCls}
              value={active.code || active.name || ''}
              onChange={(e) => {
                const item = options.find((o) => (o.code || o.name) === e.target.value);
                if (item) setActive({ code: item.code || undefined, name: item.name });
              }}
            >
              {options.map((o) => (
                <option key={o.id} value={o.code || o.name}>
                  {o.name}（{o.category}）
                </option>
              ))}
            </select>
          </div>
        )}

        {error && <div className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{error}</div>}

        {loading ? (
          <Skeleton className="h-64" />
        ) : !data || data.points.length === 0 ? (
          <Card>
            <EmptyState
              icon={<BarChart3 className="h-5 w-5" />}
              title="暂无可对比数据"
              description="至少需要两份包含同一指标的报告；上传新报告后再来看看"
              action={
                <Button size="sm" onClick={() => navigate('/reports')}>
                  去上传报告
                </Button>
              }
            />
          </Card>
        ) : (
          <>
            <Card className="mb-4 p-4">
              <div className="flex items-start justify-between">
                <div>
                  <div className="text-sm font-semibold text-slate-900">{data.name}</div>
                  <div className="mt-1 flex items-end gap-1">
                    <span className="font-numeric text-2xl font-semibold text-slate-900">
                      {data.points[data.points.length - 1]?.value_text}
                    </span>
                    <span className="pb-1 text-xs text-slate-400">{data.unit}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-slate-400">
                    参考区间 {data.ref_low ?? '-'}–{data.ref_high ?? '-'} {data.unit}
                  </div>
                </div>
                {data.delta !== null && (
                  <div
                    className={`flex items-center gap-1 rounded-full px-2 py-1 text-[11px] ${
                      data.direction === 'up'
                        ? 'bg-amber-50 text-amber-600'
                        : data.direction === 'down'
                          ? 'bg-sky-50 text-sky-600'
                          : 'bg-slate-100 text-slate-500'
                    }`}
                  >
                    <DirectionIcon className="h-3.5 w-3.5" />
                    {data.delta > 0 ? '+' : ''}
                    {data.delta} {data.unit}
                  </div>
                )}
              </div>

              <div className="mt-2">
                <ResponsiveContainer width="100%" height={220}>
                  <LineChart data={chartData} margin={{ top: 12, right: 10, left: -18, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef2f6" vertical={false} />
                    {data.ref_low !== null && data.ref_high !== null && (
                      <ReferenceArea
                        y1={data.ref_low}
                        y2={data.ref_high}
                        fill="#0d9488"
                        fillOpacity={0.06}
                        strokeOpacity={0}
                      />
                    )}
                    <XAxis
                      dataKey="label"
                      tick={{ fontSize: 10, fill: '#94a3b8' }}
                      tickLine={false}
                      axisLine={false}
                    />
                    <YAxis
                      tick={{ fontSize: 10, fill: '#94a3b8' }}
                      tickLine={false}
                      axisLine={false}
                      width={48}
                      domain={['auto', 'auto']}
                    />
                    <Tooltip
                      contentStyle={{
                        borderRadius: 12,
                        border: '1px solid #eef2f6',
                        fontSize: 12,
                      }}
                      formatter={(value) => [`${String(value)} ${data.unit}`, data.name]}
                    />
                    <Line
                      type="monotone"
                      dataKey="value"
                      stroke="#0f766e"
                      strokeWidth={2}
                      dot={{ r: 3, fill: '#0f766e' }}
                      activeDot={{ r: 5 }}
                      connectNulls
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <SectionHeader title="历史记录" subtitle={`共 ${data.total} 次检测`} />
            <Card className="divide-y divide-slate-100">
              {[...data.points].reverse().map((p) => (
                <div key={`${p.report_id}-${p.report_date}`} className="flex items-center gap-3 px-4 py-2.5">
                  <span className="w-20 shrink-0 text-[11px] text-slate-400">{p.report_date}</span>
                  <span className="font-numeric text-sm text-slate-800">
                    {p.value_text}
                    <span className="ml-0.5 text-[10px] text-slate-400">{p.unit}</span>
                  </span>
                  <span className="flex-1" />
                  <Tag
                    tone={
                      p.abnormal === 'high' ? 'amber' : p.abnormal === 'low' ? 'sky' : p.abnormal === 'normal' ? 'emerald' : 'red'
                    }
                  >
                    {p.abnormal === 'high' ? '偏高' : p.abnormal === 'low' ? '偏低' : p.abnormal === 'normal' ? '正常' : '异常'}
                  </Tag>
                  <button
                    onClick={() => navigate(`/reports/${p.report_id}`)}
                    className="text-[11px] text-primary-700"
                  >
                    报告
                  </button>
                </div>
              ))}
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
