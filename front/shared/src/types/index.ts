// 共享类型定义：与后端 API 契约一一对应

// ── 聊天 / SSE ─────────────────────────────────────────────────────────

export interface ChatRequest {
  /** 兼容字段：后端已改为从 JWT 取 user_id，此字段不再被信任 */
  user_id?: string;
  session_id: string;
  message: string;
  patient_id?: number | null;
  /** 随消息上传的图片（data URL 或裸 base64），后端转文字后并入对话；不落库 */
  image_base64?: string | null;
  /** 图片原始文件名（仅用于诊断日志） */
  image_name?: string | null;
  /** 客户端幂等键：同一消息重试时复用，服务端据此去重 */
  client_message_id?: string;
}

/** 后端 SSE 事件（POST /api/v1/chat/stream） */
export type ChatEvent =
  | { type: 'token'; content: string; seq?: number }
  | { type: 'status'; content: string; seq?: number }
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
  | { type: 'citation'; items: CitationRef[]; seq?: number }
  | { type: 'confirm_request'; data: ConfirmRequestData; seq?: number }
  | { type: 'error'; message: string; code?: string; retryable?: boolean };

/**
 * 待确认高危动作（B2：后端 clinical_actions 状态机，SSE confirm_request 下发）。
 * 后端在下发这张卡之前不会真正建预约/转诊——用户点「确认」后才执行。
 */
export interface ConfirmRequestData {
  id: number;
  /** appointment=挂号；handoff=问诊出结论后的转诊建单 */
  action_type: 'appointment' | 'handoff' | 'medication' | 'order' | string;
  status?: string;
  summary: string;
  session_id?: string | null;
  /** ISO 时间；过期后点确认会返回 410，需重新发起 */
  expires_at?: string | null;
  /** 仅白名单字段：科室/日期/时段/医生/诊断/挂号费 */
  preview?: {
    department?: string;
    date?: string;
    time_slot?: string;
    doctor_name?: string;
    /** 草稿锁定的医生账号；改选医生后后端会回传新值，为空表示待分配 */
    doctor_clinician_id?: number | null;
    diagnosis?: string | null;
    fee?: number;
  };
}

/**
 * 回答引用的知识片段定位（后端 citations 表 / SSE citation 事件）。
 * 一条 = 一次检索命中的片段，用于让用户核对"这个结论出自哪里"。
 */
export interface CitationRef {
  doc_name: string;
  doc_type?: string;
  /** 检索通道：milvus / neo4j */
  engine?: string;
  /** 页码；0 或 null 表示该文档没有页码（如纯文本指南），展示时省略 */
  page_number?: number | null;
  /** 片段序号（同一文档内的分块位置） */
  chunk_index?: number | null;
  /** 相关度分：向量相似度与 RRF 融合分并存，仅用于排序，不直接展示 */
  score?: number | null;
  /** 命中片段摘句（已截断并做过 PHI 脱敏） */
  quote?: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'handoff' | 'error';
  content: string;
  streaming?: boolean;
  stopped?: boolean;
  /** 用户消息附带的图片预览（本地 objectURL，仅本次会话可见，不随历史回放） */
  imageUrl?: string;
  /** 挂号卡片数据（role=handoff 时） */
  appointment?: AppointmentData;
  /** Safety Engine 输出审核提示（如已追加免责声明 / 已阻断） */
  safetyNotice?: string;
  safetyLevel?: 'info' | 'warn' | 'high' | 'critical';
  /** 该条回答引用的知识片段（后端 citation 事件下发） */
  citations?: CitationRef[];
  /** B2：待患者确认的高危动作（后端 confirm_request 事件下发） */
  confirm?: ConfirmRequestData;
  /** 确认卡片本地状态：pending=待操作，approved/rejected=已处理，expired=超时失效 */
  confirmState?: 'pending' | 'approved' | 'rejected' | 'expired';
  /**
   * C3：这条身子是坐席（真人）说的，不是模型说的。
   * handoff_close 是"人工环节结束"的系统收尾。仅用于气泡样式标注，不写进历史。
   */
  fromAgent?: 'human_agent' | 'handoff_close';
}

// ── 挂号 ───────────────────────────────────────────────────────────────

export interface AppointmentData {
  appointment_no: string;
  department: string;
  doctor_name: string;
  /** 接诊医生账号；为空说明这条预约没有关联到具体医生（不会自动建立接诊关系） */
  doctor_clinician_id?: number | null;
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
  /** 会话内序号（只有 PG 落库的消息才有；checkpointer 补出来的消息为 null） */
  seq?: number | null;
  /** human_agent = 坐席回复，handoff_close = 人工环节收尾，其余按普通气泡渲染 */
  event_type?: string | null;
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

// ── C4：医护端（按科室 / 责任患者授权） ───────────────────────────────

export interface ClinicianScopeInfo {
  clinician_id: number;
  roles: string[];
  department_names: string[];
  patient_ids: number[];
  patient_count: number;
  primary_department_id: number | null;
  can_decide_pending: boolean;
}

export type ClinicianAlertLevel = 'red' | 'orange' | 'yellow' | 'green';
export type ClinicianVisibleReason = 'responsible' | 'department_alert';

export interface ClinicianAlertItem {
  id: number;
  event_type: string;
  level: ClinicianAlertLevel;
  level_label: string;
  reason: string;
  symptoms: string[];
  patient_id: number | null;
  session_id: string;
  thread_id: string;
  source: string;
  created_at: string | null;
  /** 为什么这条出现在我的看板里：责任患者 / 本科室高危 */
  visible_reason: ClinicianVisibleReason;
}

export interface ClinicianAlertList {
  items: ClinicianAlertItem[];
  total: number;
  /** 候选池被打满：更早的事件没有进入过滤，列表可能不是全量 */
  truncated: boolean;
  department_names: string[];
}

export interface ClinicianPatientItem {
  patient_id: number;
  name: string;
  gender: string | null;
  age: number | null;
  relation: string;
  department_ids: number[];
  recent_departments: string[];
  latest_alert: {
    id: number;
    level: ClinicianAlertLevel;
    level_label: string;
    created_at: string | null;
  } | null;
  visible_reason: ClinicianVisibleReason;
}

export interface ClinicianPatientList {
  items: ClinicianPatientItem[];
  total: number;
}

export interface ClinicianPendingAction {
  id: number;
  action_type: string;
  status: string;
  summary: string;
  session_id: string;
  expires_at: string | null;
  decided_at: string | null;
  decided_by: string | null;
  reject_reason: string | null;
  result_ref: string | null;
  preview: Record<string, string | number | null>;
  created_at: string | null;
  /** 医护端补的归属字段（患者端不再下发，避免越权推断） */
  patient_id: number | null;
  patient_name: string;
}

export interface ClinicianPendingList {
  items: ClinicianPendingAction[];
  total: number;
  can_decide: boolean;
}

export interface ClinicianDecisionResult {
  status: string;
  result_ref: string | null;
  action_type?: string;
  data?: Record<string, unknown>;
}

// ── C3：人工接管（坐席台） ────────────────────────────────────────────────
// 工单状态机：open(待认领) → claimed(处理中) → resolved(已结单) / closed；
// cancelled 是患者撤回。后端 serialize 见 src/modules/handoff/service.py。

export type HandoffTicketStatus = 'open' | 'claimed' | 'resolved' | 'closed' | 'cancelled';

export interface HandoffTicket {
  id: number;
  ticket_no: string;
  patient_id: number;
  session_id: string;
  conversation_id: number | null;
  status: HandoffTicketStatus;
  priority: string;
  reason: string;
  summary: string;
  payload: Record<string, unknown>;
  assigned_to: number | null;
  assigned_name: string;
  claimed_at: string;
  resolved_at: string;
  resolution: string;
  message_count: number;
  last_message_at: string;
  created_at: string;
}

export interface HandoffQueue {
  items: HandoffTicket[];
  total: number;
  stats: {
    open: number;
    claimed: number;
    resolved: number;
    closed: number;
    cancelled: number;
  };
}

export interface HandoffSessionMessage {
  seq: number;
  role: string;
  content: string;
  event_type: string;
}

/**
 * 工单推送帧（GET /api/v1/handoff/stream）。
 * patient 主题：snapshot（首帧）→ ticket（状态变化）/ message（坐席或收尾正文）
 * seat 主题：ready（首帧）→ queue（"列表变了，自己重拉"）
 */
export type HandoffStreamEvent =
  | { type: 'snapshot'; items: HandoffTicket[] }
  | { type: 'ticket'; ticket: HandoffTicket }
  | {
      type: 'message';
      ticket_no: string;
      content: string;
      event_type: 'human_agent' | 'handoff_close' | string;
      message_count: number;
    }
  | { type: 'ready'; scope: string }
  | { type: 'queue'; ticket_no?: string };
