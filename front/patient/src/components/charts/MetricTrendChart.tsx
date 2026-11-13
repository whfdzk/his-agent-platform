import dayjs from 'dayjs';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { MetricPoint } from '@his/shared';

import { METRIC_LEVEL_STYLES } from '@his/shared';

interface Props {
  points: MetricPoint[];
  unit: string;
  height?: number;
  showSecondary?: boolean;
  primaryLabel?: string;
  secondaryLabel?: string;
}

/** 体征趋势图：血压双线（收缩/舒张），其它单面积线 */
export function MetricTrendChart({
  points,
  unit,
  height = 200,
  showSecondary = false,
  primaryLabel = '数值',
  secondaryLabel = '次值',
}: Props) {
  const firstDay = points[0]?.t ? dayjs(points[0].t).startOf('day') : null;
  const lastDay = points[points.length - 1]?.t ? dayjs(points[points.length - 1].t).startOf('day') : null;
  const multiDay = Boolean(firstDay && lastDay && !firstDay.isSame(lastDay));
  const data = points.map((p) => ({
    ...p,
    // 跨天显示日期，同一天显示时分，避免 14 个刻度挤成重叠文本
    label: p.t ? dayjs(p.t).format(multiDay ? 'MM-DD' : 'HH:mm') : '',
  }));
  const color =
    METRIC_LEVEL_STYLES[points[points.length - 1]?.level || 'normal']?.chart || '#0d9488';

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center text-xs text-slate-400" style={{ height }}>
        暂无测量数据
      </div>
    );
  }

  return (
    <div style={{ width: '100%', height }}>
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 8, left: -4, bottom: 0 }}>
          <defs>
            <linearGradient id="metricFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.28} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#eef2f6" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 10, fill: '#94a3b8' }}
            tickLine={false}
            axisLine={false}
            minTickGap={24}
          />
          <YAxis
            tick={{ fontSize: 10, fill: '#94a3b8' }}
            tickLine={false}
            axisLine={false}
            width={46}
            domain={['auto', 'auto']}
          />
          <Tooltip
            contentStyle={{
              borderRadius: 12,
              border: '1px solid #eef2f6',
              fontSize: 12,
              boxShadow: '0 8px 24px -12px rgba(16,24,40,.24)',
            }}
            formatter={(value, key) => [
              `${String(value)}${unit ? ` ${unit}` : ''}`,
              key === 'primary' ? primaryLabel : secondaryLabel,
            ]}
          />
          <Area
            type="monotone"
            dataKey="primary"
            stroke={color}
            strokeWidth={2}
            fill="url(#metricFill)"
            dot={{ r: 2.5, strokeWidth: 0, fill: color }}
            activeDot={{ r: 4 }}
          />
          {showSecondary && (
            <Line
              type="monotone"
              dataKey="secondary"
              stroke="#94a3b8"
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
