import type {
  Appointment,
  AppointmentData,
  AgentTrace,
  AgentStatus,
  AgentUpsert,
  AdminAgent,
  AgentCatalog,
  AdminConversation,
  AdminFeedbackItem,
  AdminMessageItem,
  AdminPrompt,
  AdminStatsOverview,
  AdminUser,
  AdminUserDetail,
  AdminUserItem,
  AdminUserUpdate,
  AuthResponse,
  AuthUser,
  ConsultationStats,
  ConsentItem,
  DataRequestItem,
  HealthProfile,
  HealthReport,
  KnowledgeDoc,
  KnowledgeFeedbackStats,
  KnowledgeNotification,
  LoginRequest,
  AdminLoginResponse,
  FailoverChainItem,
  MembershipOrderItem,
  MembershipPlanItem,
  MembershipPlanUpsert,
  MetricCreate,
  MetricTrend,
  MetricType,
  ModelConfig,
  ModelConfigUpsert,
  ModelType,
  ObservabilityStats,
  PromptVersionItem,
  PatientOverview,
  PendingChatEvent,
  SafetyEventItem,
  SafetyRuleItem,
  SafetyStats,
  SystemConfigItem,
  SystemConfigUpsert,
  SystemLimits,
  TraceListItem,
  TraceSpanItem,
  ProfileUpdate,
  RegisterRequest,
  ReminderItem,
  ReportCompare,
  ReportDetail,
  SessionSummary,
  HistoryMessage,
  SymptomStat,
  TokenUsageGroup,
  UserSubscriptionItem,
  VitalMetric,
} from '../types';

import { request, uploadForm } from './client';

// ── 登录 / 注册 ───────────────────────────────────────────────────────

export const authApi = {
  register: (body: RegisterRequest) =>
    request<AuthResponse>('/auth/register', { method: 'POST', body }),

  login: (body: LoginRequest) => request<AuthResponse>('/auth/login', { method: 'POST', body }),

  me: (token: string) =>
    request<{ user: AuthUser }>('/auth/me', { headers: { Authorization: `Bearer ${token}` } }),

  /**
   * 登出：请求后端把当前 token 加入撤销名单（幂等，无 token 也返回 204）。
   * 前端无论成功失败都必须在 finally 中清本地态，后端不可用不能导致"退不出去"。
   */
  logout: (token?: string, allDevices = false) =>
    request<void>('/auth/logout', {
      method: 'POST',
      body: { all_devices: allDevices },
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      keepalive: true,
    }),
};

// ── 管理端登录 ─────────────────────────────────────────────────────────

export const adminAuthApi = {
  login: (body: { username: string; password: string }) =>
    request<AdminLoginResponse>('/admin/auth/login', { method: 'POST', body }),

  me: () => request<{ admin: AdminUser }>('/admin/auth/me'),

  /** 管理端登出：撤销服务端 token；失败时调用方仍需清本地态 */
  logout: (token?: string, allDevices = false) =>
    request<void>('/admin/auth/logout', {
      method: 'POST',
      body: { all_devices: allDevices },
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      keepalive: true,
    }),
};

// ── 挂号 ───────────────────────────────────────────────────────────────

export const appointmentApi = {
  // 患者身份由后端从 token 解析，前端不再传 user_id（传了也会被忽略）
  list: (params: { start_date?: string; end_date?: string; status?: string; limit?: number } = {}) =>
    request<{ appointments: Appointment[]; total: number }>('/appointments', { params }),

  get: (id: number) => request<{ appointment: Appointment }>(`/appointments/${id}`),

  create: (body: {
    department: string;
    doctor_name?: string;
    date?: string;
    time_slot?: string;
    fee?: number;
    queue_number?: number;
    diagnosis?: string;
    session_id?: string;
  }) => request<{ message: string; appointment: Appointment }>('/appointments', { method: 'POST', body }),

  cancel: (id: number) =>
    request<{ message: string; appointment: Appointment }>(`/appointments/${id}/cancel`, { method: 'PATCH' }),
};

// ── 会话历史 ───────────────────────────────────────────────────────────

export const sessionApi = {
  list: (params: { user_id?: string; limit?: number }) =>
    request<{ sessions: SessionSummary[]; total: number }>('/sessions', { params }),

  messages: (sessionId: string, user_id: string) =>
    request<{ messages: HistoryMessage[]; is_inquiry_active: boolean; session_id: string }>(
      `/sessions/${encodeURIComponent(sessionId)}/messages`,
      { params: { user_id } },
    ),

  /** 断线重连：拉取已落库但客户端没收到的事件（seq 递增） */
  pendingEvents: (sessionId: string, afterSeq = 0) =>
    request<{ events: PendingChatEvent[]; has_more: boolean }>('/chat/stream/pending', {
      params: { session_id: sessionId, after_seq: afterSeq },
    }),
};

// ── 知识库 ─────────────────────────────────────────────────────────────

export const knowledgeApi = {
  listDocs: () => request<{ documents: KnowledgeDoc[]; total: number }>('/knowledge/docs'),

  upload: (file: File, docType: string, category: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('doc_type', docType);
    form.append('category', category);
    return fetch('/api/v1/knowledge/upload', { method: 'POST', body: form }).then(async (resp) => {
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(data?.detail || `上传失败 HTTP ${resp.status}`);
      }
      return (await resp.json()) as { message: string; chunks: number };
    });
  },

  deleteDoc: (docName: string) =>
    request<{ message: string }>(`/knowledge/docs/${encodeURIComponent(docName)}`, { method: 'DELETE' }),

  feedbackStats: () => request<KnowledgeFeedbackStats>('/knowledge/feedback/stats'),

  /** 提交问答反馈（👍/👎）：身份由后端从 token 解析，前端不传 user_id */
  submitFeedback: (body: {
    question: string;
    answer: string;
    rating: 1 | -1;
    comment?: string;
    intent?: string;
    channels?: string;
  }) => request<{ feedback_id: number; message: string }>('/knowledge/feedback', {
    method: 'POST',
    body,
  }),

  notifications: (category?: string) =>
    request<{ notifications: KnowledgeNotification[]; total: number }>('/knowledge/notifications', {
      params: { category },
    }),

  markNotificationsRead: (ids: number[]) =>
    request<{ message: string }>('/knowledge/notifications/read', { method: 'POST', body: { ids } }),
};

// ── 模型配置 ───────────────────────────────────────────────────────────

export const modelConfigApi = {
  list: (model_type?: ModelType) =>
    request<{ model_configs: ModelConfig[]; total: number }>('/model-configs', {
      params: { model_type },
    }),

  create: (body: ModelConfigUpsert) =>
    request<{ message: string; model_config: ModelConfig }>('/model-configs', { method: 'POST', body }),

  update: (id: number, body: ModelConfigUpsert) =>
    request<{ message: string; model_config: ModelConfig }>(`/model-configs/${id}`, { method: 'PUT', body }),

  remove: (id: number) =>
    request<{ message: string }>(`/model-configs/${id}`, { method: 'DELETE' }),

  /** 当前进程实际生效的故障转移链 */
  chain: () => request<{ chain: FailoverChainItem[]; total: number }>('/model-configs/chain'),
};

// ── 统计 / 轨迹 ────────────────────────────────────────────────────────

export const statsApi = {
  tokens: (params: {
    group_by?: 'day' | 'model' | 'worker';
    start?: string;
    end?: string;
    session_id?: string;
    user_id?: string;
  }) => request<{ groups: TokenUsageGroup[]; total: number }>('/stats/tokens', { params }),

  traces: (params: { session_id?: string; user_id?: string; worker?: string; limit?: number; offset?: number }) =>
    request<{ traces: AgentTrace[]; total: number }>('/traces', { params }),

  consultations: (days = 30) =>
    request<ConsultationStats>('/stats/consultations', { params: { days } }),

  symptoms: (top = 10) => request<{ items: SymptomStat[]; total: number }>('/stats/symptoms', { params: { top } }),
};

// ── 患者端：首页 / 档案 / 指标 / 提醒 / 隐私 ───────────────────────────

export const patientApi = {
  overview: () => request<PatientOverview>('/patient/overview'),

  profile: () => request<HealthProfile>('/patient/profile'),
  updateProfile: (body: ProfileUpdate) =>
    request<{ message: string; completeness: number }>('/patient/profile', { method: 'PUT', body }),

  metrics: (params: { metric_type?: MetricType; days?: number; limit?: number } = {}) =>
    request<{ metrics: VitalMetric[]; total: number }>('/patient/metrics', { params }),
  createMetric: (body: MetricCreate) =>
    request<{ message: string; metric: VitalMetric }>('/patient/metrics', { method: 'POST', body }),
  trends: (days = 30) =>
    request<{ trends: MetricTrend[]; days: number }>('/patient/metrics/trends', { params: { days } }),
  deleteMetric: (id: number) =>
    request<{ message: string }>(`/patient/metrics/${id}`, { method: 'DELETE' }),

  reminders: () => request<{ reminders: ReminderItem[]; total: number }>('/patient/reminders'),
  createReminder: (body: Partial<ReminderItem> & { title: string }) =>
    request<{ message: string; reminder: ReminderItem }>('/patient/reminders', { method: 'POST', body }),
  updateReminder: (id: number, body: Partial<ReminderItem>) =>
    request<{ message: string; reminder: ReminderItem }>(`/patient/reminders/${id}`, {
      method: 'PUT',
      body,
    }),
  deleteReminder: (id: number) =>
    request<{ message: string }>(`/patient/reminders/${id}`, { method: 'DELETE' }),

  consents: () => request<{ consents: ConsentItem[] }>('/patient/privacy/consents'),
  updateConsent: (scope: ConsentItem['scope'], granted: boolean, version = 'v1') =>
    request<{ message: string }>('/patient/privacy/consents', {
      method: 'PUT',
      body: { scope, granted, version },
    }),
  exportData: () =>
    request<{ message: string; request_id: number; status: string; expires_at: string; download_url: string | null }>(
      '/patient/privacy/export',
      { method: 'POST' },
    ),
  dataRequests: () => request<{ requests: DataRequestItem[] }>('/patient/privacy/requests'),
  deleteAccount: () =>
    request<{ message: string; request_id: number; status: string }>('/patient/privacy/delete', {
      method: 'POST',
    }),
};

// ── 患者端：报告中心 ───────────────────────────────────────────────────

export const reportApi = {
  list: (limit = 20) =>
    request<{ reports: HealthReport[]; total: number }>('/patient/reports', { params: { limit } }),

  detail: (id: number) => request<{ report: ReportDetail }>(`/patient/reports/${id}`),

  upload: (file: File, reportType: string, autoParse = true) => {
    const form = new FormData();
    form.append('file', file);
    form.append('report_type', reportType);
    form.append('auto_parse', String(autoParse));
    return uploadForm<{ message: string; report: ReportDetail }>('/patient/reports/upload', form);
  },

  reparse: (id: number) =>
    request<{ message: string; report: ReportDetail }>(`/patient/reports/${id}/parse`, { method: 'POST' }),

  remove: (id: number) =>
    request<{ message: string }>(`/patient/reports/${id}`, { method: 'DELETE' }),

  compare: (params: { code?: string; name?: string; limit?: number }) =>
    request<ReportCompare>('/patient/reports/compare', { params }),
};

// ── 管理端：全链路追踪 ─────────────────────────────────────────────────

export const observabilityApi = {
  traces: (params: {
    trace_id?: string;
    user_id?: string;
    session_id?: string;
    agent_name?: string;
    status?: string;
    safety_level?: string;
    days?: number;
    limit?: number;
    offset?: number;
  }) => request<{ traces: TraceListItem[]; total: number }>('/observability/traces', { params }),

  traceDetail: (traceId: string) =>
    request<{
      trace: TraceListItem | null;
      spans: TraceSpanItem[];
      events: { id: number; stage: string; level: string; action: string; rule_id: string; review_status: string; snippet: string | null; created_at: string | null }[];
    }>(`/observability/traces/${traceId}`),

  stats: (days = 7) => request<ObservabilityStats>('/observability/stats', { params: { days } }),

  retention: () =>
    request<{
      traces: number;
      spans: number;
      oldest_trace_at: string | null;
      pending_safety_reviews: number;
      trace_retention_days: number;
      safety_event_retention_days: number;
      access_log_retention_days: number;
    }>('/observability/retention'),

  purge: () =>
    request<{
      deleted_spans: number;
      deleted_traces: number;
      deleted_safety_events: number;
      trace_retention_days: number;
      safety_event_retention_days: number;
      access_log_retention_days: number;
    }>('/observability/retention/purge', { method: 'POST' }),
};

// ── 管理端：Safety Engine ──────────────────────────────────────────────

export const safetyApi = {
  events: (params: {
    level?: string;
    stage?: string;
    action?: string;
    review_status?: string;
    user_id?: string;
    days?: number;
    limit?: number;
    offset?: number;
  } = {}) => request<{ events: SafetyEventItem[]; total: number }>('/safety/events', { params }),

  pending: (limit = 50) =>
    request<{ events: SafetyEventItem[]; total: number }>('/safety/events/pending', {
      params: { limit },
    }),

  review: (id: number, decision: 'approved' | 'rejected', note?: string) =>
    request<{ message: string; event: SafetyEventItem }>(`/safety/events/${id}/review`, {
      method: 'POST',
      body: { decision, note },
    }),

  stats: (days = 30) => request<SafetyStats>('/safety/stats', { params: { days } }),

  rules: () => request<{ rules: SafetyRuleItem[]; total: number }>('/safety/rules'),

  limits: () => request<SystemLimits>('/safety/limits'),
};

// ── 管理端：用户管理 ────────────────────────────────────────────────────

export const adminUserApi = {
  list: (params: { keyword?: string; enabled?: boolean; page?: number; page_size?: number }) =>
    request<{ users: AdminUserItem[]; total: number; page: number; page_size: number }>(
      '/admin/users',
      { params: params as Record<string, string | number | boolean | undefined> },
    ),

  get: (id: number) => request<AdminUserDetail>(`/admin/users/${id}`),

  update: (id: number, body: AdminUserUpdate) =>
    request<{ message: string; user: AdminUserItem }>(`/admin/users/${id}`, { method: 'PUT', body }),

  updateStatus: (id: number, enabled: boolean) =>
    request<{ message: string; user: AdminUserItem }>(`/admin/users/${id}/status`, {
      method: 'PATCH',
      params: { enabled },
    }),
};

// ── 管理端：Agent 管理 ──────────────────────────────────────────────────

export const agentAdminApi = {
  list: (params: { agent_type?: string; status?: string } = {}) =>
    request<{ agents: AdminAgent[]; total: number }>('/admin/agents', { params }),

  get: (id: number) => request<{ agent: AdminAgent }>(`/admin/agents/${id}`),

  catalog: () => request<AgentCatalog>('/admin/agents/catalog'),

  create: (body: AgentUpsert) =>
    request<{ message: string; agent: AdminAgent }>('/admin/agents', { method: 'POST', body }),

  update: (id: number, body: AgentUpsert) =>
    request<{ message: string; agent: AdminAgent }>(`/admin/agents/${id}`, { method: 'PUT', body }),

  publish: (id: number) =>
    request<{ message: string; agent: AdminAgent }>(`/admin/agents/${id}/publish`, { method: 'POST' }),

  disable: (id: number) =>
    request<{ message: string; agent: AdminAgent }>(`/admin/agents/${id}/disable`, { method: 'POST' }),

  remove: (id: number) => request<{ message: string }>(`/admin/agents/${id}`, { method: 'DELETE' }),

  /** 把内置 Agent 补齐入库（幂等）；overwrite=true 时按内置定义覆盖现有配置 */
  syncBuiltins: (overwrite = false) =>
    request<{ message: string; created: string[]; updated: string[]; skipped: string[] }>(
      '/admin/agents/sync-builtins',
      { method: 'POST', params: { overwrite } },
    ),
};

// ── 管理端：Prompt 版本管理 ─────────────────────────────────────────────

export const promptAdminApi = {
  list: (scene?: string) =>
    request<{ prompts: AdminPrompt[]; total: number }>('/admin/prompts', { params: { scene } }),

  create: (body: {
    key: string;
    name: string;
    scene?: string;
    description?: string;
    first_content?: string;
    change_note?: string;
  }) => request<{ message: string; prompt: AdminPrompt }>('/admin/prompts', { method: 'POST', body }),

  update: (
    id: number,
    body: { name?: string; scene?: string; description?: string; enabled?: boolean },
  ) => request<{ message: string; prompt: AdminPrompt }>(`/admin/prompts/${id}`, { method: 'PUT', body }),

  remove: (id: number) => request<{ message: string }>(`/admin/prompts/${id}`, { method: 'DELETE' }),

  versions: (id: number) =>
    request<{ versions: PromptVersionItem[]; total: number }>(`/admin/prompts/${id}/versions`),

  createVersion: (id: number, body: { content: string; change_note?: string }) =>
    request<{ message: string; version: PromptVersionItem }>(`/admin/prompts/${id}/versions`, {
      method: 'POST',
      body,
    }),

  activate: (id: number, version: number) =>
    request<{ message: string; prompt: AdminPrompt }>(`/admin/prompts/${id}/versions/${version}/activate`, {
      method: 'POST',
    }),
};

// ── 管理端：对话管理 ────────────────────────────────────────────────────

export const conversationAdminApi = {
  list: (params: { keyword?: string; status?: string; user_id?: string; page?: number; page_size?: number } = {}) =>
    request<{ conversations: AdminConversation[]; total: number; page: number; page_size: number }>(
      '/admin/conversations',
      { params },
    ),

  messages: (id: number) =>
    request<{ conversation: AdminConversation; messages: AdminMessageItem[]; total: number }>(
      `/admin/conversations/${id}/messages`,
    ),

  updateStatus: (id: number, status: 'active' | 'closed') =>
    request<{ message: string; conversation: AdminConversation }>(`/admin/conversations/${id}/status`, {
      method: 'PATCH',
      body: { status },
    }),

  feedback: (params: { rating?: number; page?: number; page_size?: number } = {}) =>
    request<{ feedback: AdminFeedbackItem[]; total: number; page: number; page_size: number }>(
      '/admin/conversations/feedback/list',
      { params },
    ),
};

// ── 管理端：系统配置 ────────────────────────────────────────────────────

export const systemConfigApi = {
  list: (params: { group?: string; config_type?: string } = {}) =>
    request<{ configs: SystemConfigItem[]; total: number }>('/admin/system-configs', { params }),

  create: (body: SystemConfigUpsert) =>
    request<{ message: string; config: SystemConfigItem }>('/admin/system-configs', { method: 'POST', body }),

  update: (id: number, body: SystemConfigUpsert) =>
    request<{ message: string; config: SystemConfigItem }>(`/admin/system-configs/${id}`, {
      method: 'PUT',
      body,
    }),

  remove: (id: number) => request<{ message: string }>(`/admin/system-configs/${id}`, { method: 'DELETE' }),
};

// ── 管理端：统计总览（P1） ──────────────────────────────────────────────

export const adminStatsApi = {
  overview: (days = 7) =>
    request<AdminStatsOverview>('/admin/stats/overview', { params: { days } }),
};

// ── 管理端：会员管理（P2） ──────────────────────────────────────────────

export const membershipAdminApi = {
  plans: () => request<{ plans: MembershipPlanItem[]; total: number }>('/admin/memberships/plans'),

  createPlan: (body: MembershipPlanUpsert) =>
    request<{ message: string; plan: MembershipPlanItem }>('/admin/memberships/plans', { method: 'POST', body }),

  updatePlan: (id: number, body: MembershipPlanUpsert) =>
    request<{ message: string; plan: MembershipPlanItem }>(`/admin/memberships/plans/${id}`, {
      method: 'PUT',
      body,
    }),

  removePlan: (id: number) =>
    request<{ message: string }>(`/admin/memberships/plans/${id}`, { method: 'DELETE' }),

  orders: (params: { status?: string; user_id?: string; page?: number; page_size?: number } = {}) =>
    request<{ orders: MembershipOrderItem[]; total: number; page: number; page_size: number }>(
      '/admin/memberships/orders',
      { params },
    ),

  /** 人工核销订单并开通会员（对接支付前） */
  createOrder: (body: { user_id: string; plan_id: number }) =>
    request<{ message: string; order_no: string }>('/admin/memberships/orders', { method: 'POST', body }),

  subscriptions: () =>
    request<{ subscriptions: UserSubscriptionItem[]; total: number }>(
      '/admin/memberships/subscriptions',
    ),
};
