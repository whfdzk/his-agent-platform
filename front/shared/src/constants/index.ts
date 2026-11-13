// 紧急程度映射（中文 UI 文案 + 颜色 class）
export const URGENCY_LABELS: Record<string, { label: string; color: string }> = {
  normal: { label: '普通', color: 'text-emerald-600 bg-emerald-50' },
  urgent: { label: '较急', color: 'text-amber-600 bg-amber-50' },
  emergency: { label: '紧急', color: 'text-red-600 bg-red-50' },
};

// 模型类型中文映射
export const MODEL_TYPE_LABELS: Record<string, string> = {
  embedding: '嵌入模型',
  chat: '聊天模型',
  rerank: '重排模型',
  multimodal: '多模态',
};

// 知识库文档类型选项
export const DOC_TYPE_OPTIONS = [
  { value: 'guideline', label: '诊疗指南' },
  { value: 'drug_instruction', label: '药品说明' },
  { value: 'sop', label: '医院 SOP' },
  { value: 'literature', label: '医学文献' },
];

// worker 显示名
export const WORKER_LABELS: Record<string, string> = {
  supervisor: '总调度',
  inquiry: '智慧问诊',
  drug: '药物咨询',
  knowledge: '知识问答',
  report: '报告解读',
  operation: '运营数据',
  memory: '记忆',
};

// ── 患者端：体征指标 ───────────────────────────────────────────────────

export const METRIC_OPTIONS = [
  { value: 'blood_pressure', label: '血压', unit: 'mmHg', primaryLabel: '收缩压', secondaryLabel: '舒张压' },
  { value: 'blood_glucose', label: '血糖', unit: 'mmol/L', primaryLabel: '血糖值', secondaryLabel: '' },
  { value: 'weight', label: '体重', unit: 'kg', primaryLabel: '体重', secondaryLabel: '' },
  { value: 'heart_rate', label: '心率', unit: '次/分', primaryLabel: '心率', secondaryLabel: '' },
] as const;

export const METRIC_LEVEL_STYLES: Record<string, { label: string; badge: string; dot: string; chart: string }> = {
  normal: { label: '正常', badge: 'bg-emerald-50 text-emerald-600', dot: 'bg-emerald-500', chart: '#10b981' },
  low: { label: '偏低', badge: 'bg-sky-50 text-sky-600', dot: 'bg-sky-500', chart: '#0ea5e9' },
  high: { label: '偏高', badge: 'bg-amber-50 text-amber-600', dot: 'bg-amber-500', chart: '#f59e0b' },
  critical: { label: '危急', badge: 'bg-red-50 text-red-600', dot: 'bg-red-500', chart: '#ef4444' },
};

export const GLUCOSE_PERIODS = [
  { value: 'fasting', label: '空腹' },
  { value: 'postprandial', label: '餐后 2h' },
  { value: 'random', label: '随机' },
];

export const REPORT_TYPE_OPTIONS = [
  { value: 'lab', label: '检验报告' },
  { value: 'imaging', label: '影像报告' },
  { value: 'physical', label: '体检报告' },
  { value: 'other', label: '其他' },
];

export const REPORT_STATUS_STYLES: Record<string, { label: string; badge: string }> = {
  uploaded: { label: '待解析', badge: 'bg-slate-100 text-slate-500' },
  parsing: { label: '解析中', badge: 'bg-amber-50 text-amber-600' },
  parsed: { label: '已解析', badge: 'bg-emerald-50 text-emerald-600' },
  failed: { label: '解析失败', badge: 'bg-red-50 text-red-600' },
};
