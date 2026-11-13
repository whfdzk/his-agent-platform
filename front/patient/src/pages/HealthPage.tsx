import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import dayjs from 'dayjs';
import {
  Activity,
  BellRing,
  BookOpen,
  Droplets,
  HeartPulse,
  Loader2,
  Plus,
  Scale,
  Sparkles,
  Trash2,
} from 'lucide-react';
import { METRIC_LEVEL_STYLES, METRIC_OPTIONS, patientApi, streamMetricAdvice } from '@his/shared';
import type { MetricCreate, MetricTrend, MetricType, ReminderItem, VitalMetric } from '@his/shared';

import { MetricTrendChart } from '../components/charts/MetricTrendChart';
import {
  Button,
  Card,
  EmptyState,
  Field,
  SectionHeader,
  Sheet,
  Skeleton,
  Tag,
  inputCls,
} from '../components/ui';

const ICONS: Record<MetricType, typeof Activity> = {
  blood_pressure: HeartPulse,
  blood_glucose: Droplets,
  weight: Scale,
  heart_rate: Activity,
};

/**
 * 解读正文渲染：后端流式输出按 【结论】/【可能的风险】/【值得注意】/【何时就医】 分段，
 * 以 "- " 开头的是条目。逐行渲染能边流边显示，也避免引入 Markdown 依赖。
 */
function AdviceText({ text }: { text: string }) {
  const lines = text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);

  return (
    <div className="space-y-1.5">
      {lines.map((line, idx) => {
        if (line.startsWith('【')) {
          const end = line.indexOf('】');
          const title = end > 0 ? line.slice(1, end) : line.slice(1);
          const rest = end > 0 ? line.slice(end + 1).trim() : '';
          return (
            <div key={idx} className={idx === 0 ? '' : 'pt-1.5'}>
              <div className="text-[11px] font-medium text-slate-400">{title}</div>
              {rest && (
                <div className="mt-0.5 text-xs leading-relaxed text-slate-700">{rest}</div>
              )}
            </div>
          );
        }
        if (line.startsWith('- ')) {
          return (
            <div key={idx} className="flex gap-1.5 text-xs leading-relaxed text-slate-600">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-300" />
              <span>{line.slice(2)}</span>
            </div>
          );
        }
        return (
          <p key={idx} className="text-xs leading-relaxed text-slate-600">
            {line}
          </p>
        );
      })}
    </div>
  );
}

export function HealthPage() {
  const [tab, setTab] = useState<MetricType>('blood_pressure');
  const [trends, setTrends] = useState<MetricTrend[]>([]);
  const [metrics, setMetrics] = useState<VitalMetric[]>([]);
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [addOpen, setAddOpen] = useState(false);
  const [reminderOpen, setReminderOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  // 单条记录的风险解读面板（SSE 流式）
  const [adviceOpen, setAdviceOpen] = useState(false);
  const [adviceTarget, setAdviceTarget] = useState<VitalMetric | null>(null);
  const [adviceMeta, setAdviceMeta] = useState<{ label: string; disclaimer: string } | null>(null);
  const [adviceText, setAdviceText] = useState('');
  const [adviceSources, setAdviceSources] = useState<{ doc_name: string; page: number | null }[]>([]);
  const [adviceGrounded, setAdviceGrounded] = useState<boolean | null>(null);
  const [adviceNotice, setAdviceNotice] = useState('');
  const [adviceLoading, setAdviceLoading] = useState(false);
  const [adviceError, setAdviceError] = useState('');
  const adviceAbort = useRef<AbortController | null>(null);

  const [form, setForm] = useState({
    primary: '',
    secondary: '',
    period: 'fasting',
    measured_at: dayjs().format('YYYY-MM-DDTHH:mm'),
    note: '',
  });
  const [reminderForm, setReminderForm] = useState({
    title: '测量血压',
    metric_type: 'blood_pressure' as MetricType,
    remind_time: '08:00',
    frequency: 'daily' as 'daily' | 'weekly',
  });

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [trendRes, metricRes, reminderRes] = await Promise.all([
        patientApi.trends(30),
        patientApi.metrics({ metric_type: tab, days: 90, limit: 100 }),
        patientApi.reminders(),
      ]);
      setTrends(trendRes.trends);
      setMetrics(metricRes.metrics);
      setReminders(reminderRes.reminders);
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [tab]);

  useEffect(() => {
    void load();
  }, [load]);

  const currentTrend = useMemo(() => trends.find((t) => t.metric_type === tab), [trends, tab]);
  const option = METRIC_OPTIONS.find((o) => o.value === tab)!;
  const isBp = tab === 'blood_pressure';
  // 没有记录时不能回落成 normal：会显示"正常"误导用户（此时根本没测过）
  const latestLevel = currentTrend?.latest?.level;
  const style = latestLevel ? METRIC_LEVEL_STYLES[latestLevel] : null;
  // 血压均值要把收缩/舒张分别平均，单看收缩压会误导
  const avgText = useMemo(() => {
    if (!currentTrend) return '--';
    if (!isBp) return String(currentTrend.avg);
    const pts = currentTrend.points;
    if (!pts.length) return '--';
    const sys = pts.reduce((s, p) => s + p.primary, 0) / pts.length;
    const dia = pts.reduce((s, p) => s + (p.secondary ?? 0), 0) / pts.length;
    return `${Math.round(sys)}/${Math.round(dia)}`;
  }, [currentTrend, isBp]);

  /** 打开解读面板：立即展示该条记录，再流式接收知识库解读 */
  const openAdvice = useCallback(async (m: VitalMetric) => {
    adviceAbort.current?.abort();
    const ctrl = new AbortController();
    adviceAbort.current = ctrl;

    setAdviceTarget(m);
    setAdviceOpen(true);
    setAdviceMeta(null);
    setAdviceText('');
    setAdviceSources([]);
    setAdviceGrounded(null);
    setAdviceNotice('');
    setAdviceError('');
    setAdviceLoading(true);

    try {
      await streamMetricAdvice(m.id, {
        token: localStorage.getItem('his_token') || undefined,
        signal: ctrl.signal,
        onEvent: (ev) => {
          if (ev.type === 'meta') {
            setAdviceMeta({ label: ev.label, disclaimer: ev.disclaimer });
          } else if (ev.type === 'sources') {
            setAdviceSources(ev.sources);
            setAdviceGrounded(ev.grounded);
          } else if (ev.type === 'token') {
            setAdviceText((prev) => prev + ev.content);
          } else if (ev.type === 'notice') {
            setAdviceNotice(ev.message);
          } else if (ev.type === 'error') {
            setAdviceError(ev.message);
          } else if (ev.type === 'done') {
            setAdviceGrounded(ev.grounded);
          }
        },
      });
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setAdviceError((e as Error).message || '解读失败，请稍后重试');
      }
    } finally {
      if (!ctrl.signal.aborted) setAdviceLoading(false);
    }
  }, []);

  // 离开页面时中断未完成的解读，避免后台挂着一条 SSE
  useEffect(() => () => adviceAbort.current?.abort(), []);

  const submitMetric = async () => {
    const primary = Number(form.primary);
    if (!Number.isFinite(primary) || primary <= 0) {
      setError('请填写有效数值');
      return;
    }
    if (isBp && (!form.secondary || Number(form.secondary) <= 0)) {
      setError('请填写舒张压');
      return;
    }
    setSaving(true);
    setError('');
    try {
      const payload: MetricCreate = {
        metric_type: tab,
        value_primary: primary,
        value_secondary: isBp ? Number(form.secondary) : undefined,
        unit: option.unit,
        period: tab === 'blood_glucose' ? form.period : undefined,
        note: form.note || undefined,
        measured_at: form.measured_at ? dayjs(form.measured_at).toISOString() : undefined,
      };
      await patientApi.createMetric(payload);
      setAddOpen(false);
      setForm({ ...form, primary: '', secondary: '', note: '' });
      await load();
    } catch (e) {
      setError((e as Error).message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const submitReminder = async () => {
    setSaving(true);
    try {
      await patientApi.createReminder({
        title: reminderForm.title,
        metric_type: reminderForm.metric_type,
        remind_time: reminderForm.remind_time,
        frequency: reminderForm.frequency,
      });
      setReminderOpen(false);
      await load();
    } catch (e) {
      setError((e as Error).message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-5 md:pb-10 2xl:max-w-5xl">
        <header className="mb-4 flex items-end justify-between">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">健康管理</h1>
            <p className="mt-0.5 text-xs text-slate-400">记录体征，观察趋势，按时提醒</p>
          </div>
          <Button size="sm" onClick={() => setAddOpen(true)}>
            <Plus className="h-3.5 w-3.5" /> 记录
          </Button>
        </header>

        {/* 指标切换 */}
        <div className="mb-4 grid grid-cols-4 gap-1 rounded-2xl bg-white p-1 shadow-card">
          {METRIC_OPTIONS.map((o) => {
            const Icon = ICONS[o.value as MetricType];
            const active = tab === o.value;
            return (
              <button
                key={o.value}
                onClick={() => setTab(o.value as MetricType)}
                className={`flex flex-col items-center gap-1 rounded-xl py-2 text-[11px] transition-colors ${
                  active ? 'bg-primary-700 text-white shadow-sm' : 'text-slate-500 hover:bg-slate-50'
                }`}
              >
                <Icon className="h-4 w-4" />
                {o.label}
              </button>
            );
          })}
        </div>

        {error && <div className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{error}</div>}

        {/* 最新值 + 趋势 */}
        <Card className="mb-4 p-4">
          <div className="flex items-start justify-between">
            <div>
              <div className="text-[11px] text-slate-400">最新{option.label}</div>
              <div className="mt-1 flex items-end gap-1">
                <span className="font-numeric text-3xl font-semibold text-slate-900">
                  {currentTrend?.latest
                    ? isBp
                      ? `${currentTrend.latest.primary}/${currentTrend.latest.secondary ?? '-'}`
                      : currentTrend.latest.primary
                    : '--'}
                </span>
                <span className="pb-1 text-xs text-slate-400">{option.unit}</span>
              </div>
              <div className="mt-1.5 flex items-center gap-2">
                {style && (
                  <span className={`rounded-full px-2 py-0.5 text-[10px] ${style.badge}`}>{style.label}</span>
                )}
                {currentTrend?.latest?.t && (
                  <span className="text-[11px] text-slate-400">
                    {dayjs(currentTrend.latest.t).format('MM-DD HH:mm')}
                  </span>
                )}
              </div>
            </div>
            <div className="text-right text-[11px] text-slate-400">
              <div>30 天均值</div>
              <div className="font-numeric mt-0.5 text-base font-semibold text-slate-700">
                {avgText}
              </div>
              <div className="mt-1">记录 {currentTrend?.count ?? 0} 次</div>
            </div>
          </div>

          <div className="mt-2">
            {currentTrend && currentTrend.points.length > 0 ? (
              <MetricTrendChart
                points={currentTrend.points}
                unit={option.unit}
                showSecondary={isBp}
                primaryLabel={option.primaryLabel}
                secondaryLabel={option.secondaryLabel}
              />
            ) : (
              <div className="flex h-[160px] flex-col items-center justify-center gap-2 text-xs text-slate-400">
                最近 30 天没有{option.label}记录
                <Button size="sm" variant="secondary" onClick={() => setAddOpen(true)}>
                  记录一次
                </Button>
              </div>
            )}
          </div>

          {currentTrend && currentTrend.points.length > 0 && (
            <div className="mt-2 grid grid-cols-3 divide-x divide-slate-100 border-t border-slate-100 pt-3 text-center">
              <div>
                <div className="text-[10px] text-slate-400">最低</div>
                <div className="font-numeric text-sm text-slate-700">{currentTrend.min}</div>
              </div>
              <div>
                <div className="text-[10px] text-slate-400">最高</div>
                <div className="font-numeric text-sm text-slate-700">{currentTrend.max}</div>
              </div>
              <div>
                <div className="text-[10px] text-slate-400">异常次数</div>
                <div className="font-numeric text-sm text-amber-600">{currentTrend.abnormal_count}</div>
              </div>
            </div>
          )}
        </Card>

        {/* 历史记录 */}
        <div className="mb-5">
          <SectionHeader title="历史记录" subtitle={`最近 ${metrics.length} 条`} />
          <Card className="divide-y divide-slate-100">
            {loading ? (
              <div className="space-y-2 p-3">
                <Skeleton className="h-10" />
                <Skeleton className="h-10" />
              </div>
            ) : metrics.length === 0 ? (
              <EmptyState
                icon={<Activity className="h-5 w-5" />}
                title={`还没有${option.label}记录`}
                description="建议每天固定时间测量，数据越连续趋势越准确"
              />
            ) : (
              metrics.slice(0, 12).map((m) => {
                const s = METRIC_LEVEL_STYLES[m.level];
                return (
                  <div key={m.id} className="flex items-center gap-3 px-4 py-3">
                    <span className={`h-2 w-2 shrink-0 rounded-full ${s.dot}`} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-1">
                        <span className="font-numeric text-sm font-medium text-slate-800">
                          {m.metric_type === 'blood_pressure'
                            ? `${m.value_primary}/${m.value_secondary ?? '-'}`
                            : m.value_primary}
                        </span>
                        <span className="text-[10px] text-slate-400">{m.unit}</span>
                        {m.period && (
                          <span className="text-[10px] text-slate-400">
                            · {m.period === 'fasting' ? '空腹' : m.period === 'postprandial' ? '餐后' : '随机'}
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-slate-400">
                        {dayjs(m.measured_at || undefined).format('YYYY-MM-DD HH:mm')}
                        {m.note ? ` · ${m.note}` : ''}
                      </div>
                    </div>
                    <Tag tone={m.level === 'normal' ? 'emerald' : m.level === 'critical' ? 'red' : 'amber'}>
                      {s.label}
                    </Tag>
                    <button
                      onClick={() => void openAdvice(m)}
                      className="flex shrink-0 items-center gap-1 rounded-lg bg-primary-50 px-2 py-1 text-[10px] font-medium text-primary-700 transition-colors hover:bg-primary-100"
                      aria-label={`解读这条${option.label}记录的风险`}
                      title="查询知识库，解读这条数值的风险与注意事项"
                    >
                      <Sparkles className="h-3 w-3" />
                      解读
                    </button>
                    <button
                      onClick={async () => {
                        await patientApi.deleteMetric(m.id).catch(() => undefined);
                        await load();
                      }}
                      className="text-slate-300 hover:text-red-500"
                      aria-label="删除"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </div>
                );
              })
            )}
          </Card>
        </div>

        {/* 提醒 */}
        <div className="mb-4">
          <SectionHeader
            title="测量提醒"
            subtitle="按时间提醒测量与服药"
            action={
              <Button size="sm" variant="secondary" onClick={() => setReminderOpen(true)}>
                <Plus className="h-3.5 w-3.5" /> 新增
              </Button>
            }
          />
          <Card className="divide-y divide-slate-100">
            {reminders.length === 0 ? (
              <EmptyState
                icon={<BellRing className="h-5 w-5" />}
                title="还没有提醒"
                description="例如每天早上 8 点测量血压"
              />
            ) : (
              reminders.map((r) => (
                <div key={r.id} className="flex items-center gap-3 px-4 py-3">
                  <span className="font-numeric w-12 text-xs text-slate-500">{r.remind_time}</span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-slate-700">{r.title}</div>
                    <div className="text-[10px] text-slate-400">
                      {r.frequency === 'daily' ? '每天' : '每周'} ·{' '}
                      {r.metric_type ? METRIC_OPTIONS.find((o) => o.value === r.metric_type)?.label : '通用'}
                    </div>
                  </div>
                  <button
                    onClick={async () => {
                      await patientApi.updateReminder(r.id, { enabled: !r.enabled }).catch(() => undefined);
                      await load();
                    }}
                    className={`relative h-5 w-9 rounded-full transition-colors ${
                      r.enabled ? 'bg-primary-600' : 'bg-slate-200'
                    }`}
                    aria-label="启用/停用"
                  >
                    <span
                      className={`absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all ${
                        r.enabled ? 'left-[18px]' : 'left-0.5'
                      }`}
                    />
                  </button>
                  <button
                    onClick={async () => {
                      await patientApi.deleteReminder(r.id).catch(() => undefined);
                      await load();
                    }}
                    className="text-slate-300 hover:text-red-500"
                    aria-label="删除提醒"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))
            )}
          </Card>
        </div>

        <div className="rounded-2xl bg-white/60 p-3 text-[10px] leading-relaxed text-slate-400">
          参考区间：血压 &lt;140/90 mmHg，空腹血糖 3.9–6.1 mmol/L，心率 60–100 次/分。
          数值仅作健康管理提示，异常请就医。
        </div>
      </div>

      {/* 记录弹窗 */}
      <Sheet
        open={addOpen}
        title={`记录${option.label}`}
        onClose={() => setAddOpen(false)}
        footer={
          <Button block onClick={submitMetric} disabled={saving}>
            {saving ? '保存中…' : '保存记录'}
          </Button>
        }
      >
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <Field label={option.primaryLabel}>
              <input
                className={inputCls}
                inputMode="decimal"
                value={form.primary}
                onChange={(e) => setForm({ ...form, primary: e.target.value })}
                placeholder={isBp ? '120' : '5.6'}
              />
            </Field>
            {isBp && (
              <Field label="舒张压">
                <input
                  className={inputCls}
                  inputMode="decimal"
                  value={form.secondary}
                  onChange={(e) => setForm({ ...form, secondary: e.target.value })}
                  placeholder="80"
                />
              </Field>
            )}
          </div>
          {tab === 'blood_glucose' && (
            <Field label="测量时段">
              <select
                className={inputCls}
                value={form.period}
                onChange={(e) => setForm({ ...form, period: e.target.value })}
              >
                <option value="fasting">空腹</option>
                <option value="postprandial">餐后 2 小时</option>
                <option value="random">随机</option>
              </select>
            </Field>
          )}
          <Field label="测量时间">
            <input
              type="datetime-local"
              className={inputCls}
              value={form.measured_at}
              onChange={(e) => setForm({ ...form, measured_at: e.target.value })}
            />
          </Field>
          <Field label="备注（可选）">
            <input
              className={inputCls}
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
              placeholder="如：运动后、服药前"
              maxLength={60}
            />
          </Field>
        </div>
      </Sheet>

      {/* 风险解读弹窗：知识库检索 + 流式建议 */}
      <Sheet
        open={adviceOpen}
        title={`风险解读${adviceTarget ? ` · ${METRIC_OPTIONS.find((o) => o.value === adviceTarget.metric_type)?.label ?? ''}` : ''}`}
        onClose={() => {
          adviceAbort.current?.abort();
          setAdviceOpen(false);
        }}
      >
        <div className="space-y-3">
          {adviceTarget && (
            <div className="flex items-center gap-2 rounded-xl bg-slate-50 px-3 py-2">
              <span className="font-numeric text-sm font-semibold text-slate-800">
                {adviceTarget.metric_type === 'blood_pressure'
                  ? `${adviceTarget.value_primary}/${adviceTarget.value_secondary ?? '-'}`
                  : adviceTarget.value_primary}
              </span>
              <span className="text-[10px] text-slate-400">{adviceTarget.unit}</span>
              <span className="text-[10px] text-slate-400">
                {dayjs(adviceTarget.measured_at || undefined).format('MM-DD HH:mm')}
              </span>
              <span className="ml-auto">
                <Tag
                  tone={
                    adviceTarget.level === 'normal'
                      ? 'emerald'
                      : adviceTarget.level === 'critical'
                        ? 'red'
                        : 'amber'
                  }
                >
                  {METRIC_LEVEL_STYLES[adviceTarget.level]?.label ?? adviceTarget.level}
                </Tag>
              </span>
            </div>
          )}

          {adviceError && (
            <div className="space-y-3">
              <div className="rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{adviceError}</div>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => adviceTarget && void openAdvice(adviceTarget)}
              >
                重新解读
              </Button>
            </div>
          )}

          {adviceText ? (
            <AdviceText text={adviceText} />
          ) : (
            !adviceError && (
              <div className="space-y-2 py-2">
                <Skeleton className="h-4" />
                <Skeleton className="h-4 w-5/6" />
                <Skeleton className="h-4 w-2/3" />
                <div className="flex items-center gap-1.5 pt-2 text-[11px] text-slate-400">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  正在查询知识库并生成解读…
                </div>
              </div>
            )
          )}

          {adviceLoading && adviceText && (
            <div className="flex items-center gap-1.5 text-[10px] text-slate-400">
              <Loader2 className="h-3 w-3 animate-spin" /> 生成中…
            </div>
          )}

          {adviceSources.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-slate-400">
              <BookOpen className="h-3 w-3" />
              知识库参考：
              {adviceSources.map((s) => (
                <span key={s.doc_name} className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-500">
                  {s.doc_name}
                  {s.page ? ` 第${s.page}页` : ''}
                </span>
              ))}
            </div>
          )}
          {adviceGrounded === false && (
            <p className="text-[10px] text-slate-400">
              知识库中暂未检索到对应文档，以上为通用健康建议。
            </p>
          )}
          {adviceNotice && <p className="text-[10px] text-amber-600">{adviceNotice}</p>}
          <p className="text-[10px] leading-relaxed text-slate-400">
            {adviceMeta?.disclaimer ?? '以上仅为健康管理提示，不能替代医生诊断；如有不适请及时就医。'}
          </p>
        </div>
      </Sheet>

      {/* 提醒弹窗 */}
      <Sheet
        open={reminderOpen}
        title="新增测量提醒"
        onClose={() => setReminderOpen(false)}
        footer={
          <Button block onClick={submitReminder} disabled={saving}>
            {saving ? '保存中…' : '创建提醒'}
          </Button>
        }
      >
        <div className="space-y-3">
          <Field label="提醒内容">
            <input
              className={inputCls}
              value={reminderForm.title}
              onChange={(e) => setReminderForm({ ...reminderForm, title: e.target.value })}
              placeholder="如：测量晨起血压"
            />
          </Field>
          <Field label="关联指标">
            <select
              className={inputCls}
              value={reminderForm.metric_type}
              onChange={(e) =>
                setReminderForm({ ...reminderForm, metric_type: e.target.value as MetricType })
              }
            >
              {METRIC_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="提醒时间">
              <input
                type="time"
                className={inputCls}
                value={reminderForm.remind_time}
                onChange={(e) => setReminderForm({ ...reminderForm, remind_time: e.target.value })}
              />
            </Field>
            <Field label="频率">
              <select
                className={inputCls}
                value={reminderForm.frequency}
                onChange={(e) =>
                  setReminderForm({ ...reminderForm, frequency: e.target.value as 'daily' | 'weekly' })
                }
              >
                <option value="daily">每天</option>
                <option value="weekly">每周</option>
              </select>
            </Field>
          </div>
        </div>
      </Sheet>
    </div>
  );
}

export default HealthPage;
