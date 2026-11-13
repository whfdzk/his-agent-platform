import { useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Activity, Coins, MessagesSquare, Users } from 'lucide-react';
import {
  adminStatsApi,
  knowledgeApi,
  statsApi,
  type AdminStatsOverview,
  type ConsultationStats,
  type SymptomStat,
  type TokenUsageGroup,
} from '@his/shared';

const PIE_COLORS = ['#2b7fff', '#22c55e', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#84cc16'];

function StatCard({ label, value, icon: Icon, tone }: { label: string; value: string | number; icon: typeof Coins; tone: string }) {
  return (
    <div className="flex min-w-0 items-center gap-3.5 rounded-lg border border-slate-200/80 bg-white p-4 shadow-sm shadow-slate-900/[0.02] sm:p-5">
      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-lg ${tone}`}>
        <Icon className="h-[18px] w-[18px]" />
      </div>
      <div className="min-w-0">
        <div className="text-xs text-slate-500">{label}</div>
        <div className="mt-1 truncate text-lg font-semibold tabular-nums text-slate-900 sm:text-xl">{value}</div>
      </div>
    </div>
  );
}

export function DashboardPage() {
  const [consultStats, setConsultStats] = useState<ConsultationStats | null>(null);
  const [symptoms, setSymptoms] = useState<SymptomStat[]>([]);
  const [tokenToday, setTokenToday] = useState(0);
  const [docCount, setDocCount] = useState(0);
  const [overview, setOverview] = useState<AdminStatsOverview | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      statsApi.consultations(30).catch(() => null),
      statsApi.symptoms(10).catch(() => null),
      statsApi.tokens({ group_by: 'day' }).catch(() => null),
      knowledgeApi.listDocs().catch(() => null),
      adminStatsApi.overview(7).catch(() => null),
    ]).then(([c, s, t, d, ov]) => {
      if (c) setConsultStats(c);
      if (s) setSymptoms(s.items);
      if (t) {
        const groups = t.groups as TokenUsageGroup[];
        const today = new Date().toISOString().slice(0, 10);
        const todayGroup = groups.find((g) => g.key === today);
        const allToday = groups.filter((g) => g.key === today);
        setTokenToday(allToday.reduce((sum, g) => sum + g.total_tokens, 0) || todayGroup?.total_tokens || 0);
      }
      if (d) setDocCount(d.total);
      if (ov) setOverview(ov);
      setLoading(false);
    });
  }, []);

  return (
    <div className="space-y-5 md:space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs font-medium text-primary-600">平台运营</p>
          <h1 className="mt-1 text-xl font-semibold text-slate-900">数据看板</h1>
        </div>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span className="h-2 w-2 rounded-full bg-emerald-500" /> 数据概览
        </div>
      </div>

      {/* 指标卡：用户量 / 调用量 / Token 消耗（P1 数据统计） */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="注册用户"
          value={overview ? `${overview.overview.total_users}（启用 ${overview.overview.enabled_users}）` : '-'}
          icon={Users}
          tone="bg-sky-50 text-sky-700"
        />
        <StatCard
          label="累计 LLM 调用"
          value={overview ? overview.overview.total_calls.toLocaleString() : '-'}
          icon={Activity}
          tone="bg-indigo-50 text-indigo-600"
        />
        <StatCard
          label="累计 Token 消耗"
          value={overview ? overview.overview.total_tokens.toLocaleString() : tokenToday.toLocaleString()}
          icon={Coins}
          tone="bg-amber-50 text-amber-600"
        />
        <StatCard
          label="会话 / 消息"
          value={overview ? `${overview.overview.total_conversations} / ${overview.overview.total_messages}` : docCount}
          icon={MessagesSquare}
          tone="bg-emerald-50 text-emerald-700"
        />
      </div>

      {loading ? (
        <div className="rounded-lg border border-slate-200/80 bg-white p-10 text-center text-sm text-slate-400">
          加载中…
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {/* 问诊量趋势 */}
          <div className="min-w-0 rounded-lg border border-slate-200/80 bg-white p-4 shadow-sm shadow-slate-900/[0.02] sm:p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-800">问诊量趋势（30 天）</h2>
            <ResponsiveContainer width="100%" height={240}>
              <LineChart data={consultStats?.by_day ?? []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e8eef2" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} tickFormatter={(v: string) => v.slice(5)} />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip />
                <Line type="monotone" dataKey="count" name="问诊量" stroke="#178bb3" strokeWidth={2.5} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* 科室分布 */}
          <div className="min-w-0 rounded-lg border border-slate-200/80 bg-white p-4 shadow-sm shadow-slate-900/[0.02] sm:p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-800">科室分布</h2>
            <ResponsiveContainer width="100%" height={240}>
              <PieChart>
                <Pie
                  data={consultStats?.by_department ?? []}
                  dataKey="count"
                  nameKey="name"
                  innerRadius={50}
                  outerRadius={85}
                  paddingAngle={2}
                >
                  {(consultStats?.by_department ?? []).map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
              </PieChart>
            </ResponsiveContainer>
          </div>

          {/* 症状 TopN */}
          <div className="min-w-0 rounded-lg border border-slate-200/80 bg-white p-4 shadow-sm shadow-slate-900/[0.02] sm:p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-800">常见症状 Top 10</h2>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={symptoms} layout="vertical">
                <CartesianGrid strokeDasharray="3 3" stroke="#e8eef2" />
                <XAxis type="number" tick={{ fontSize: 11 }} allowDecimals={false} />
                <YAxis type="category" dataKey="name" width={70} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="count" name="出现次数" fill="#22c55e" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* 紧急程度分布 */}
          <div className="min-w-0 rounded-lg border border-slate-200/80 bg-white p-4 shadow-sm shadow-slate-900/[0.02] sm:p-5">
            <h2 className="mb-4 text-sm font-semibold text-slate-800">紧急程度分布</h2>
            <ResponsiveContainer width="100%" height={240}>
              <BarChart data={consultStats?.by_urgency ?? []}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e8eef2" />
                <XAxis dataKey="level" tick={{ fontSize: 12 }} />
                <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count" name="问诊量" fill="#f59e0b" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}
