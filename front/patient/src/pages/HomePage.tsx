import { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import dayjs from 'dayjs';
import {
  Activity,
  ArrowRight,
  BellRing,
  CalendarDays,
  ChevronRight,
  FileText,
  HeartPulse,
  MessageCircle,
  Plus,
  ShieldAlert,
  Sparkles,
  Upload,
  UserRound,
} from 'lucide-react';
import { patientApi } from '@his/shared';
import type { MetricType, PatientOverview, VitalMetric } from '@his/shared';
import { METRIC_LEVEL_STYLES } from '@his/shared';

import { useUserSession } from '../store/session';
import { Button, Card, EmptyState, Ring, SectionHeader, Skeleton, Tag } from '../components/ui';

const QUICK_ACTIONS = [
  { to: '/consult', label: 'AI 咨询', icon: MessageCircle, tone: 'bg-primary-50 text-primary-700' },
  { to: '/reports', label: '上传报告', icon: Upload, tone: 'bg-cyan-50 text-cyan-700' },
  { to: '/profile', label: '健康档案', icon: UserRound, tone: 'bg-sky-50 text-sky-700' },
  { to: '/health', label: '健康管理', icon: Activity, tone: 'bg-amber-50 text-amber-600' },
];

const METRIC_LABEL: Record<MetricType, string> = {
  blood_pressure: '血压',
  blood_glucose: '血糖',
  weight: '体重',
  heart_rate: '心率',
};

function metricValue(m?: VitalMetric): string {
  if (!m) return '--';
  if (m.metric_type === 'blood_pressure') {
    return `${m.value_primary}/${m.value_secondary ?? '-'}`;
  }
  return String(m.value_primary);
}

export function HomePage() {
  const { token, user } = useUserSession();
  const navigate = useNavigate();
  const [data, setData] = useState<PatientOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    if (!token) {
      setData(null);
      return;
    }
    setLoading(true);
    setError('');
    try {
      setData(await patientApi.overview());
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const greeting = (() => {
    const h = new Date().getHours();
    if (h < 6) return '夜深了';
    if (h < 11) return '早上好';
    if (h < 14) return '中午好';
    if (h < 18) return '下午好';
    return '晚上好';
  })();

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-5xl px-4 pb-24 pt-5 md:px-6 md:pb-10 md:pt-7 lg:px-7 2xl:max-w-6xl">
        {/* 顶部问候 */}
        <header className="mb-4 flex items-center justify-between">
          <div>
            <div className="text-xs text-slate-400">
              {dayjs().format('YYYY 年 M 月 D 日')} · 周{'日一二三四五六'[dayjs().day()]}
            </div>
            <h1 className="mt-1 text-xl font-semibold tracking-tight text-slate-900">
              {greeting}，{user?.name || '朋友'}
            </h1>
          </div>
          <Link to="/me" className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white py-1 pl-1 pr-3 transition hover:border-primary-200 hover:bg-primary-50/40">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-700 text-xs font-medium text-white">
              {(user?.name || '游').slice(0, 1)}
            </span>
            <span className="text-xs text-slate-600">{user ? '我的' : '登录'}</span>
          </Link>
        </header>

        {/* 健康概览 */}
        <Card className="mb-4 overflow-hidden p-0">
          <div className="flex items-center justify-between px-4 pt-4">
            <div>
              <div className="text-sm font-semibold text-slate-900">健康概览</div>
              <div className="mt-0.5 text-[11px] text-slate-400">
                {data?.latest_metrics && Object.keys(data.latest_metrics).length > 0
                  ? `最近更新 ${dayjs(
                      Object.values(data.latest_metrics)[0]?.measured_at || undefined,
                    ).format('MM-DD HH:mm')}`
                  : '记录体征，获得个性化建议'}
              </div>
            </div>
            {data && (
              <div className="flex items-center gap-2">
                <Ring percent={data.profile.completeness} size={56} label="档案" />
              </div>
            )}
          </div>

          {!token ? (
            <div className="px-4 pb-4 pt-3">
          <div className="rounded-lg border border-primary-100 bg-primary-50/70 p-4">
                <div className="text-sm font-medium text-primary-800">登录后查看健康数据</div>
                <p className="mt-1 text-xs leading-relaxed text-primary-700/80">
                  同步问诊记录、检查报告与体征趋势，AI 解读更准确。
                </p>
                <Button size="sm" className="mt-3" onClick={() => navigate('/login')}>
                  立即登录
                </Button>
              </div>
            </div>
          ) : loading && !data ? (
            <div className="grid grid-cols-3 gap-2 p-4">
              <Skeleton className="h-20" />
              <Skeleton className="h-20" />
              <Skeleton className="h-20" />
            </div>
          ) : (
            <div className="grid grid-cols-3 divide-x divide-slate-100 px-1 py-3">
              {(['blood_pressure', 'blood_glucose', 'weight'] as MetricType[]).map((type) => {
                const m = data?.latest_metrics?.[type];
                const style = METRIC_LEVEL_STYLES[m?.level || 'normal'];
                return (
                  <button
                    key={type}
                    onClick={() => navigate('/health')}
                    className="flex flex-col items-center gap-1 px-2 py-1 text-center"
                  >
                    <span className="text-[11px] text-slate-400">{METRIC_LABEL[type]}</span>
                    <span className="font-numeric text-xl font-semibold text-slate-900">
                      {metricValue(m)}
                    </span>
                    <span className="text-[10px] text-slate-400">{m?.unit || (type === 'weight' ? 'kg' : '')}</span>
                    <span className={`rounded-full px-1.5 py-0.5 text-[10px] ${style.badge}`}>
                      {m ? style.label : '未记录'}
                    </span>
                  </button>
                );
              })}
            </div>
          )}

          {data && data.risk_alerts.length > 0 && (
            <div className="border-t border-slate-100 px-4 py-3">
              <div className="flex items-start gap-2 rounded-xl bg-amber-50/70 px-3 py-2">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
                <div className="min-w-0 text-[11px] leading-relaxed text-amber-700">
                  {data.risk_alerts.map((a) => a.message).join('；')}
                  <button onClick={() => navigate('/health')} className="ml-1 underline">
                    查看详情
                  </button>
                </div>
              </div>
            </div>
          )}
        </Card>

        {error && <div className="mb-4 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{error}</div>}

        {/* AI 咨询入口 */}
        <button
          onClick={() => navigate('/consult')}
          className="mb-5 w-full rounded-lg border border-primary-800 bg-primary-800 p-4 text-left transition-colors hover:bg-primary-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600/40 focus-visible:ring-offset-2"
        >
          <div className="relative flex items-start gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-white/15 bg-white/10">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5 text-sm font-semibold text-white">
                智能健康助手
                <span className="rounded-sm border border-white/20 px-1.5 py-0.5 text-[9px] font-medium text-primary-50">
                  7×24 在线
                </span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-primary-100">
                描述症状获取分诊建议，上传报告让 AI 帮你划重点。
              </p>
            </div>
            <ArrowRight className="mt-1 h-4 w-4 shrink-0 text-white/80" />
          </div>
        </button>

        {/* 快捷入口 */}
        <div className="mb-5 grid grid-cols-2 gap-2 sm:grid-cols-4">
          {QUICK_ACTIONS.map(({ to, label, icon: Icon, tone }) => (
            <Link
              key={to}
              to={to}
              className="flex flex-col items-center gap-1.5 rounded-lg border border-slate-200 bg-white py-3 transition-colors hover:border-primary-200 hover:bg-primary-50/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-600/30"
            >
              <span className={`flex h-9 w-9 items-center justify-center rounded-xl ${tone}`}>
                <Icon className="h-[18px] w-[18px]" />
              </span>
              <span className="text-[11px] text-slate-600">{label}</span>
            </Link>
          ))}
        </div>

        <div className="mb-5 grid grid-cols-1 gap-5 lg:grid-cols-2">
          {/* 今日提醒 */}
          <section className="min-w-0">
            <SectionHeader
              title="今日提醒"
              subtitle={data?.today_reminders.length ? `${data.today_reminders.length} 项待完成` : '养成记录习惯'}
              action={
                <Link to="/health" className="flex items-center text-[11px] text-primary-700">
                  管理 <ChevronRight className="h-3 w-3" />
                </Link>
              }
            />
            <Card className="divide-y divide-slate-100">
              {!data || data.today_reminders.length === 0 ? (
                <div className="flex items-center gap-3 px-4 py-4">
                  <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-50 text-slate-300">
                    <BellRing className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-xs font-medium text-slate-700">还没有测量提醒</div>
                    <div className="text-[11px] text-slate-400">设置提醒，血压血糖不漏测</div>
                  </div>
                  <Button size="sm" variant="secondary" onClick={() => navigate('/health')}>
                    <Plus className="h-3.5 w-3.5" /> 添加
                  </Button>
                </div>
              ) : (
                data.today_reminders.slice(0, 3).map((r) => (
                  <div key={r.id} className="flex items-center gap-3 px-4 py-3">
                    <span className="font-numeric w-11 text-xs text-slate-500">{r.remind_time}</span>
                    <span className="h-6 w-px bg-slate-100" />
                    <span className="flex-1 text-xs text-slate-700">{r.title}</span>
                    <Tag tone="teal">待记录</Tag>
                  </div>
                ))
              )}
            </Card>
          </section>

          {/* 最近报告 */}
          <section className="min-w-0">
          <SectionHeader
            title="最近报告"
            subtitle={data ? `共 ${data.reports_total} 份 · ${data.abnormal_reports} 份有异常` : '上传报告自动解析'}
            action={
              <Link to="/reports" className="flex items-center text-[11px] text-primary-700">
                全部 <ChevronRight className="h-3 w-3" />
              </Link>
            }
          />
          {data && data.reports.length > 0 ? (
            <div className="space-y-2">
              {data.reports.map((r) => (
                <Link
                  key={r.id}
                  to={`/reports/${r.id}`}
                  className="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3 transition-colors hover:border-primary-200 hover:bg-primary-50/20"
                >
                  <span className="flex h-9 w-9 items-center justify-center rounded-md bg-cyan-50 text-cyan-700">
                    <FileText className="h-4 w-4" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-slate-800">{r.file_name}</div>
                    <div className="text-[11px] text-slate-400">
                      {dayjs(r.created_at).format('YYYY-MM-DD')} · {r.metric_count} 项指标
                    </div>
                  </div>
                  {r.abnormal_count > 0 ? (
                    <Tag tone="amber">{r.abnormal_count} 项异常</Tag>
                  ) : (
                    <Tag tone="emerald">未见异常</Tag>
                  )}
                  <ChevronRight className="h-4 w-4 shrink-0 text-slate-300" />
                </Link>
              ))}
            </div>
          ) : (
            <Card>
              <EmptyState
                icon={<FileText className="h-5 w-5" />}
                title="还没有上传报告"
                description="支持 PDF / 图片，上传后自动提取指标并标记异常"
                action={
                  <Button size="sm" onClick={() => navigate('/reports')}>
                    <Upload className="h-3.5 w-3.5" /> 上传报告
                  </Button>
                }
              />
            </Card>
          )}
          </section>
        </div>

        {/* 下次就诊 */}
        <div className="mb-3">
          <SectionHeader title="就诊安排" />
          <Card className="flex items-center gap-3 px-4 py-4">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary-50 text-primary-700">
              {data?.next_appointment ? <CalendarDays className="h-5 w-5" /> : <HeartPulse className="h-5 w-5" />}
            </span>
            {data?.next_appointment ? (
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-slate-800">
                  {data.next_appointment.department} · {data.next_appointment.doctor_name}
                </div>
                <div className="text-[11px] text-slate-400">
                  {data.next_appointment.date} {data.next_appointment.time_slot} · 序号{' '}
                  {data.next_appointment.appointment_no}
                </div>
              </div>
            ) : (
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium text-slate-800">暂无就诊安排</div>
                <div className="text-[11px] text-slate-400">问诊后可直接预约挂号</div>
              </div>
            )}
            <Link to="/appointments" className="text-[11px] text-primary-700">
              我的预约
            </Link>
          </Card>
        </div>

        <p className="mt-6 text-center text-[10px] leading-relaxed text-slate-400">
          AI 提供健康科普与分诊建议，不能替代医生诊断。急症请立即拨打 120。
        </p>
      </div>
    </div>
  );
}
