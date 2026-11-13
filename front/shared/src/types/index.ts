// 共享类型定义：与后端 API 契约一一对应

// ── 聊天 / SSE ─────────────────────────────────────────────────────────

export interface ChatRequest {
  /** 兼容字段：后端已改为从 JWT 取 user_id，此字段不再被信任 */
  user_id?: string;
  session_id: string;
  message: string;
  patient_id?: number | null;
  /** 客户端幂等键：同一消息重试时复用，服务端据此去重 */
  client_message_id?: string;
}

/** 后端 SSE 事件（POST /api/v1/chat/stream） */
export type ChatEvent =
  | { type: 'token'; content: string; seq?: number }
  | { type: 'clarify'; question: string; seq?: number }
  | { type: 'appointment'; data: AppointmentData; seq?: number }
  | {
      type: 'safety';
      level: 'info' | 'warn' | 'high' | 'critical';
      notice?: string;
      rules?: string[];
      /** 输出被纠正时给出的最终正文，前端需替换已渲染的消息 */
      content?: string;
      seq?: number;
    }
  | { type: 'done'; session_id: string }
  | { type: 'error'; message: string; code?: string; retryable?: boolean };

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'handoff' | 'error';
  content: string;
  streaming?: boolean;
  stopped?: boolean;
  /** 挂号卡片数据（role=handoff 时） */
  appointment?: AppointmentData;
  /** Safety Engine 输出审核提示（如已追加免责声明 / 已阻断） */
  safetyNotice?: string;
  safetyLevel?: 'info' | 'warn' | 'high' | 'critical';
}

// ── 挂号 ───────────────────────────────────────────────────────────────

export interface AppointmentData {
  appointment_no: string;
  department: string;
  doctor_name: string;
  date: string;
  time_slot: string;
  fee: number;
  queue_number: number;
  diagnosis?: string | null;
}

export interface Appointment extends AppointmentData {
  id: number;
  patient_id: number | null;
  session_id: string | null;
  status: 'confirmed' | 'cancelled';
  created_at: string | null;
}

// ── 会话 ───────────────────────────────────────────────────────────────

export interface SessionSummary {
  session_id: string;
  user_id: string;
  title: string;
  is_inquiry_active: boolean;
  last_active_at: string | null;
}

export interface HistoryMessage {
  role: 'user' | 'assistant';
  content: string;
}

/** 断线重连时拉取的已落库事件 */
export interface PendingChatEvent {
  type: string;
  seq: number;
  role?: 'user' | 'assistant';
  content?: string;
  data?: AppointmentData;
}

// ── 知识库 ─────────────────────────────────────────────────────────────

export interface KnowledgeDoc {
  doc_name: string;
  doc_type: string;
  category: string;
}

export interface KnowledgeFeedbackStats {
  total?: number;
  positive?: number;
  negative?: number;
  [k: string]: unknown;
}

export interface KnowledgeNotification {
  id: number;
  doc_name: string;
  doc_type: string;
  category: string;
  action: string;
  is_read: boolean;
}

// ── 模型配置 ───────────────────────────────────────────────────────────

export type ModelType = 'embedding' | 'chat' | 'rerank' | 'multimodal';

export interface ModelConfig {
  id: number;
  model_type: ModelType;
  name: string;
  base_url: string;
  api_key: string;
  has_api_key: boolean;
  model_name: string;
  enabled: boolean;
  is_default: boolean;
  /** 故障转移顺序：数字越小越先被尝试（主模型 is_default 永远排最前） */
  priority: number;
  created_at: string | null;
}

export interface ModelConfigUpsert {
  model_type: ModelType;
  name: string;
  base_url: string;
  api_key?: string;
  model_name: string;
  enabled: boolean;
  is_default: boolean;
  priority?: number;
}

/** 运行时实际生效的故障转移链 */
export interface FailoverChainItem {
  order: number;
  name: string;
  model_name: string;
  base_url: string;
}

// ── 管理端账号 ─────────────────────────────────────────────────────────

export interface AdminUser {
  id: string | number;
  username: string;
  display_name?: string;
  role: string;
}

export interface AdminLoginResponse {
  message: string;
  token: string;
  admin: AdminUser;
}

// ── Token 用量 / 轨迹 ──────────────────────────────────────────────────

export interface TokenUsageGroup {
  key: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
  avg_latency_ms: number;
}

export interface TraceItem {
  worker: string;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  latency_ms: number;
}

export interface AgentTrace {
  turn_id: string;
  session_id: string | null;
  user_id: string | null;
  created_at: string | null;
  total_tokens: number;
  items: TraceItem[];
}

// ── 登录 / 注册 ───────────────────────────────────────────────────────

export interface AuthUser {
  id: number;
  name: string;
  phone: string;
  gender: string | null;
  age: number | null;
}

export interface LoginRequest {
  phone: string;
  password: string;
}

export interface RegisterRequest {
  name: string;
  phone: string;
  password: string;
  gender?: string | null;
  age?: number | null;
}

export interface AuthResponse {
  message: string;
  token: string;
  user: AuthUser;
}

// ── 问诊统计 ───────────────────────────────────────────────────────────

export interface ConsultationStats {
  days: number;
  by_day: { date: string; count: number }[];
  by_department: { name: string; count: number }[];
  by_urgency: { level: string; count: number }[];
}

export interface SymptomStat {
  name: string;
  count: number;
}

// ── 患者端：体征指标 ───────────────────────────────────────────────────

export type MetricType = 'blood_pressure' | 'blood_glucose' | 'weight' | 'heart_rate';
export type MetricLevel = 'normal' | 'low' | 'high' | 'critical';

export interface VitalMetric {
  id: number;
  metric_type: MetricType;
  label: string;
  value_primary: number;
  value_secondary: number | null;
  unit: string;
  period: string | null;
  level: MetricLevel;
  level_label: string;
  source: string;
  note: string | null;
  measured_at: string | null;
}

export interface MetricPoint {
  t: string | null;
  primary: number;
  secondary: number | null;
  level: MetricLevel;
}

export interface MetricTrend {
  metric_type: MetricType;
  label: string;
  unit: string;
  points: MetricPoint[];
  count: number;
  avg: number;
  max: number;
  min: number;
  latest: MetricPoint;
  abnormal_count: number;
}

export interface MetricCreate {
  metric_type: MetricType;
  value_primary: number;
  value_secondary?: number | null;
  unit?: string;
  period?: string;
  note?: string;
  measured_at?: string;
}

/** 单条体征记录的风险解读事件（SSE 流） */
export type MetricAdviceEvent =
  | {
      type: 'meta';
      metric_id: number | null;
      label: string;
      value_text: string;
      level: MetricLevel;
      level_label: string;
      disclaimer: string;
    }
  | { type: 'sources'; sources: { doc_name: string; page: number | null }[]; grounded: boolean }
  | { type: 'token'; content: string }
  | { type: 'notice'; message: string }
  | { type: 'error'; message: string; retryable: boolean }
  | { type: 'done'; grounded: boolean };

// ── 患者端：健康档案 ───────────────────────────────────────────────────

export interface ConsentItem {
  scope: 'ai_consult' | 'data_share' | 'research' | 'marketing';
  label: string;
  granted: boolean;
  version: string;
  granted_at: string | null;
  revoked_at: string | null;
}

export interface HealthProfile {
  patient_id: number;
  name: string;
  gender: string | null;
  age: number | null;
  phone_masked: string;
  birth_date: string | null;
  blood_type: string | null;
  height_cm: number | null;
  weight_kg: number | null;
  waist_cm: number | null;
  bmi: number | null;
  bmi_level: string;
  marital_status: string | null;
  occupation: string | null;
  city: string | null;
  emergency_contact_name: string | null;
  emergency_contact_phone_masked: string;
  allergy_history: string | null;
  medical_history: string | null;
  surgery_history: string | null;
  family_history: string | null;
  chronic_diseases: string | null;
  smoking: string | null;
  drinking: string | null;
  completeness: number;
  consents: ConsentItem[];
}

export type ProfileUpdate = Partial<
  Omit<
    HealthProfile,
    | 'patient_id'
    | 'phone_masked'
    | 'emergency_contact_phone_masked'
    | 'bmi'
    | 'bmi_level'
    | 'completeness'
    | 'consents'
  >
>;

// ── 患者端：首页概览 ───────────────────────────────────────────────────

export interface ReminderItem {
  id: number;
  title: string;
  metric_type: MetricType | null;
  remind_time: string;
  frequency: 'daily' | 'weekly';
  weekdays: string | null;
  enabled: boolean;
  note: string | null;
}

export interface PatientOverview {
  profile: {
    name: string;
    gender: string | null;
    age: number | null;
    blood_type: string | null;
    completeness: number;
  };
  bmi: number | null;
  bmi_level: string;
  latest_metrics: Partial<Record<MetricType, VitalMetric>>;
  risk_alerts: { metric_type: MetricType; label: string; level: MetricLevel; message: string }[];
  reports: {
    id: number;
    file_name: string;
    report_type: string;
    status: string;
    abnormal_count: number;
    metric_count: number;
    created_at: string | null;
  }[];
  reports_total: number;
  abnormal_reports: number;
  consultations_total: number;
  today_reminders: { id: number; title: string; remind_time: string; metric_type: MetricType | null }[];
  next_appointment: {
    appointment_no: string;
    department: string;
    doctor_name: string;
    date: string;
    time_slot: string;
  } | null;
}

// ── 患者端：报告中心 ───────────────────────────────────────────────────

export interface ReportMetricItem {
  id: number;
  name: string;
  code: string | null;
  category: string;
  value_text: string;
  value_num: number | null;
  unit: string;
  ref_low: number | null;
  ref_high: number | null;
  ref_text: string | null;
  abnormal: 'normal' | 'high' | 'low' | 'abnormal';
}

export interface HealthReport {
  id: number;
  file_name: string;
  report_type: 'lab' | 'imaging' | 'physical' | 'other';
  hospital: string | null;
  report_date: string | null;
  status: 'uploaded' | 'parsing' | 'parsed' | 'failed';
  parse_engine: string | null;
  summary: string | null;
  abnormal_count: number;
  metric_count: number;
  error_message: string | null;
  created_at: string | null;
  parsed_at: string | null;
}

export interface ReportDetail extends HealthReport {
  metrics: ReportMetricItem[];
}

export interface ReportCompare {
  name: string;
  code: string | null;
  unit: string;
  ref_low: number | null;
  ref_high: number | null;
  points: {
    report_id: number;
    report_date: string | null;
    value_num: number | null;
    value_text: string;
    unit: string;
    ref_low: number | null;
    ref_high: number | null;
    abnormal: string;
  }[];
  delta: number | null;
  direction: 'up' | 'down' | 'flat';
  total: number;
}

// ── 患者端：隐私与数据 ─────────────────────────────────────────────────

export interface DataRequestItem {
  id: number;
  request_type: 'export' | 'delete';
  status: string;
  note: string | null;
  created_at: string | null;
  expires_at: string | null;
}

// ── 管理端：全链路追踪 ─────────────────────────────────────────────────

export interface TraceSpanItem {
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: 'api' | 'agent' | 'tool' | 'llm' | 'retrieval' | 'safety' | 'final_response' | 'persistence';
  agent_name: string | null;
  agent_version: string | null;
  tool_name: string | null;
  model_name: string | null;
  model_version: string | null;
  status: string;
  error_type: string | null;
  latency_ms: number;
  tokens: { prompt: number; completion: number; total: number };
  attributes: Record<string, unknown>;
  created_at: string | null;
}

export interface TraceListItem {
  trace_id: string;
  request_id: string;
  user_id: string;
  session_id: string;
  turn_id: string;
  entrypoint: string;
  agent_name: string;
  agent_version: string;
  prompt_version: string;
  model_name: string;
  model_version: string;
  status: string;
  error_type: string | null;
  latency_ms: number;
  tokens: { prompt: number; completion: number; total: number };
  tool_calls: number;
  tool_names: string[];
  agent_calls: number;
  retrieval: { count: number; sources: Record<string, unknown>[] };
  safety: { level: string; result: string; rules: string[] };
  input_chars: number;
  output_chars: number;
  created_at: string | null;
}

export interface ObservabilityStats {
  days: number;
  total: number;
  errors: number;
  blocked: number;
  error_rate: number;
  avg_latency_ms: number;
  total_tokens: number;
  avg_tool_calls: number;
  avg_retrieval: number;
  by_agent: { agent: string; count: number; avg_latency_ms: number; tokens: number }[];
  by_model: { model: string; count: number; tokens: number }[];
  safety_distribution: { level: string; count: number }[];
}

// ── 管理端：Safety Engine ──────────────────────────────────────────────

export interface SafetyEventItem {
  id: number;
  trace_id: string;
  request_id: string;
  user_id: string;
  session_id: string;
  stage: 'input' | 'output' | 'tool';
  level: 'info' | 'warn' | 'high' | 'critical';
  action: 'allow' | 'warn' | 'block' | 'escalate';
  rule_id: string;
  rule_name: string;
  category: string;
  snippet: string | null;
  requires_review: boolean;
  review_status: 'none' | 'pending' | 'approved' | 'rejected';
  reviewer_id: string | null;
  review_note: string | null;
  reviewed_at: string | null;
  created_at: string | null;
}

export interface SafetyStats {
  days: number;
  total: number;
  pending_review: number;
  by_level: { level: string; count: number }[];
  by_category: { category: string; count: number }[];
  by_action: { action: string; count: number }[];
  top_rules: { rule_id: string; rule_name: string; count: number }[];
}

export interface SafetyRuleItem {
  id: string;
  name: string;
  category: string;
  level: string;
  action: string;
  stage: string;
  requires_review: boolean;
}

export interface SystemLimits {
  agent_max_calls: number;
  tool_max_calls: number;
  turn_max_tokens: number;
  turn_timeout_seconds: number;
  retrieval_max_results: number;
  user_max_concurrent_tasks: number;
  safety_enabled: boolean;
  trace_retention_days: number;
  safety_event_retention_days: number;
  access_log_retention_days: number;
}

// ── 管理端：用户管理 ────────────────────────────────────────────────────

export interface AdminUserItem {
  id: number;
  name: string;
  phone: string;
  gender: string | null;
  age: number | null;
  id_card: string | null;
  blood_type: string | null;
  allergy_history: string | null;
  medical_history: string | null;
  enabled: boolean;
  registered: boolean;
  conversations: number;
  appointments: number;
  created_at: string | null;
}

export interface AdminUserUpdate {
  name?: string;
  gender?: string;
  age?: number;
  blood_type?: string;
  allergy_history?: string;
  medical_history?: string;
}

/** 患者详情聚合（管理端只读视图） */
export interface AdminUserDetail {
  user: AdminUserItem;
  profile: {
    birth_date: string | null;
    height_cm: number | null;
    weight_kg: number | null;
    waist_cm: number | null;
    marital_status: string | null;
    occupation: string | null;
    city: string | null;
    emergency_contact_name: string | null;
    emergency_contact_phone: string | null;
    surgery_history: string | null;
    family_history: string | null;
    chronic_diseases: string | null;
    smoking: string | null;
    drinking: string | null;
  };
  recent_conversations: {
    id: number;
    session_id: string;
    title: string;
    status: string;
    last_active_at: string | null;
  }[];
  recent_appointments: {
    id: number;
    appointment_no: string;
    department: string;
    doctor_name: string;
    date: string;
    time_slot: string;
    status: string;
  }[];
  membership: {
    plan_name: string;
    start_at: string | null;
    expire_at: string | null;
    status: string;
  } | null;
}

// ── 管理端：Agent 管理 ──────────────────────────────────────────────────

export type AgentStatus = 'draft' | 'published' | 'disabled';

/** Agent 运行时配置（存 agents.config JSONB，后端按此结构校验） */
export interface AgentSummarizationConfig {
  enabled: boolean;
  trigger_tokens: number;
  trigger_messages: number;
  keep_messages: number;
}

export interface AgentRuntimeConfig {
  prompt_id: number | null;
  system_prompt: string;
  model_config_id: number | null;
  model: string;
  temperature: number | null;
  max_turns: number;
  tool_ids: string[];
  skill_ids: string[];
  sub_agent_codes: string[];
  degrade_message: string;
  summarization: AgentSummarizationConfig;
}

export interface AdminAgent {
  id: number;
  code: string;
  name: string;
  agent_type: 'supervisor' | 'worker';
  description: string;
  avatar: string;
  status: AgentStatus;
  /** database=库配置已生效（已发布）；builtin=运行时仍用代码内置默认 */
  config_source: 'database' | 'builtin';
  config: Record<string, unknown> | null;
  published_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AgentUpsert {
  code: string;
  name: string;
  agent_type: 'supervisor' | 'worker';
  description: string;
  avatar: string;
  config?: Record<string, unknown> | null;
}

export interface AgentCatalogTool {
  id: string;
  name: string;
  description: string;
  category: string;
}

export interface AgentCatalogSkill {
  id: string;
  name: string;
  description: string;
  tool_ids: string[];
}

/** Supervisor 可调度的 worker */
export interface AgentCatalogSubAgent {
  code: string;
  name: string;
}

export interface AgentCatalog {
  tools: AgentCatalogTool[];
  skills: AgentCatalogSkill[];
  sub_agents: AgentCatalogSubAgent[];
}

// ── 管理端：Prompt 版本管理 ─────────────────────────────────────────────

export interface AdminPrompt {
  id: number;
  key: string;
  name: string;
  scene: string;
  description: string;
  enabled: boolean;
  active_version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface PromptVersionItem {
  id: number;
  prompt_id: number;
  version: number;
  content: string;
  change_note: string;
  created_by: string;
  created_at: string | null;
}

// ── 管理端：对话管理 ────────────────────────────────────────────────────

export interface AdminConversation {
  id: number;
  user_id: string;
  session_id: string;
  title: string;
  status: 'active' | 'closed';
  message_count: number;
  last_active_at: string | null;
  created_at: string | null;
}

export interface AdminMessageItem {
  id: number;
  seq: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  event_type: string;
  turn_id: string | null;
  created_at: string | null;
}

export interface AdminFeedbackItem {
  id: number;
  user_id: string;
  question: string;
  answer: string;
  rating: 1 | -1 | 0;
  comment: string;
  intent: string;
  created_at: string | null;
}

// ── 管理端：系统配置 ────────────────────────────────────────────────────

export interface SystemConfigItem {
  id: number;
  key: string;
  value: string | boolean | string[];
  raw_value: string;
  config_type: 'param' | 'switch' | 'dict';
  group: string;
  name: string;
  description: string;
  enabled: boolean;
  updated_at: string | null;
}

export interface SystemConfigUpsert {
  key: string;
  value: string;
  config_type: 'param' | 'switch' | 'dict';
  group: string;
  name: string;
  description: string;
  enabled: boolean;
}

// ── 管理端：统计总览（P1） ──────────────────────────────────────────────

export interface AdminStatsOverview {
  overview: {
    total_users: number;
    enabled_users: number;
    total_conversations: number;
    total_messages: number;
    total_calls: number;
    total_tokens: number;
  };
  usage_trend: { date: string; calls: number; tokens: number }[];
  user_trend: { date: string; count: number }[];
  days: number;
}

// ── 管理端：会员管理（P2） ──────────────────────────────────────────────

export interface MembershipPlanItem {
  id: number;
  name: string;
  price_cents: number;
  duration_days: number;
  benefits: string[];
  enabled: boolean;
  sort_order: number;
  created_at: string | null;
}

export interface MembershipPlanUpsert {
  name: string;
  price_cents: number;
  duration_days: number;
  benefits: string[];
  enabled: boolean;
  sort_order: number;
}

export interface MembershipOrderItem {
  id: number;
  order_no: string;
  user_id: string;
  plan_id: number;
  plan_name: string;
  amount_cents: number;
  status: 'pending' | 'paid' | 'cancelled' | 'refunded';
  created_at: string | null;
}

export interface UserSubscriptionItem {
  id: number;
  user_id: string;
  plan_name: string;
  start_at: string | null;
  expire_at: string | null;
  status: 'active' | 'expired';
}
