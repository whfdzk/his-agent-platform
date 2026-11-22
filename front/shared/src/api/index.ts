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
  ConfirmRequestData,
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
  ClinicianAlertList,
  ClinicianDecisionResult,
  ClinicianPatientList,
  ClinicianPendingList,
  ClinicianScopeInfo,
  HandoffQueue,
  HandoffTicket,
  HandoffSessionMessage,
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

  /**
   * 可挂号医生目录：只返回"有档案 + 在岗 + 有 clinician 角色"的医生。
   * 不传 department_id 就是全院在岗医生（患者端按科室自己筛）。
   */
  doctors: (params: { department_id?: number } = {}) =>
    request<{
      doctors: {
        clinician_id: number;
        name: string;
        title: string;
        specialty: string;
        intro: string;
        departments: { id: number; name: string; is_primary: boolean }[];
      }[];
      total: number;
    }>('/appointments/doctors', { params }),

  create: (body: {
    department: string;
    doctor_name?: string;
    /** 目录里选中的医生账号：带上它，确认后该医生才会自动成为你的接诊医生 */
    doctor_clinician_id?: number;
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

// ── B2：高危动作确认（clinical_actions 状态机） ─────────────────────────
// 后端在下发 confirm_request 卡片时只落了"待确认"草稿，号源未占用；
// 用户点确认才走执行器落库。重复确认幂等返回原结果，不会建两条预约。

export const clinicalActionApi = {
  /** 当前账号待确认的动作（刷新页面后可恢复卡片） */
  pending: () => request<{ items: ConfirmRequestData[] }>('/clinical-actions/pending'),

  approve: (id: number) =>
    request<{
      status: string;
      result_ref?: string | null;
      action_type?: string;
      data?: AppointmentData;
    }>(`/clinical-actions/${id}/approve`, { method: 'POST' }),

  reject: (id: number, reason = '患者拒绝') =>
    request<{ status: string; reject_reason?: string | null }>(`/clinical-actions/${id}/reject`, {
      method: 'POST',
      body: { reason },
    }),

  /** 确认前改选医生（只改待确认草稿；doctor_clinician_id=0 表示改回待分配） */
  retargetDoctor: (id: number, doctorClinicianId: number) =>
    request<{ item: ConfirmRequestData }>(`/clinical-actions/${id}/appointment-doctor`, {
      method: 'POST',
      body: { doctor_clinician_id: doctorClinicianId },
    }),
};

// ── C4：医护端（科室 / 责任患者授权） ─────────────────────────────────
// 全部传 authResetOn403: false：这里的 403 是业务拒绝（如"该患者不在你的责任范围"），
// 不该把医生踢下线；真正的凭证失效仍然是 401，由 client 统一处理。

export const clinicianApi = {
  scope: () =>
    request<ClinicianScopeInfo>('/clinician/scope', { authResetOn403: false }),

  patients: (params: { limit?: number } = {}) =>
    request<ClinicianPatientList>('/clinician/patients', {
      params,
      authResetOn403: false,
    }),

  alerts: (params: { level?: string; min_level?: string; patient_id?: number; session_id?: string; limit?: number } = {}) =>
    request<ClinicianAlertList>('/clinician/emergency-events', {
      params,
      authResetOn403: false,
    }),

  pending: (params: { limit?: number } = {}) =>
    request<ClinicianPendingList>('/clinician/pending-actions', {
      params,
      authResetOn403: false,
    }),

  approve: (id: number) =>
    request<ClinicianDecisionResult>(`/clinician/pending-actions/${id}/approve`, {
      method: 'POST',
      authResetOn403: false,
    }),

  reject: (id: number) =>
    request<{ status: string; reject_reason?: string | null }>(
      `/clinician/pending-actions/${id}/reject`,
      { method: 'POST', authResetOn403: false },
    ),
};

// ── 管理端：医护授权（C4） ──────────────────────────────────────────────
// 把"授予 clinician 角色 / 绑科室 / 分责任患者"搬进界面。授权判定仍在后端
// src/modules/clinician/scope.py，这里只是写关系，不做前端判断。

export interface AdminClinicianDepartment {
  id: number;
  name: string;
  is_primary: boolean;
}

export interface AdminClinicianPatient {
  patient_id: number;
  name: string;
  relation: string;
  granted_by: string;
}

export interface AdminClinicianProfile {
  employee_no: string;
  title: string;
  specialty: string;
  intro: string;
  status: string;
  note: string;
}

export interface AdminClinicianItem {
  clinician_id: number;
  name: string;
  phone: string;
  enabled: boolean;
  roles: string[];
  can_decide: boolean;
  departments: AdminClinicianDepartment[];
  patient_count: number;
  granted_by: string;
  /** 空对象 = 还没建档（无档案的医生不会出现在患者端可挂号目录里） */
  profile: Partial<AdminClinicianProfile>;
}

export interface AdminClinicianDetail extends AdminClinicianItem {
  patients: AdminClinicianPatient[];
}

/** 档案变更审计：谁在什么时候把哪些字段从什么改成什么 */
export interface AdminAuditEntry {
  id: number;
  action: string;
  actor: string;
  actor_name: string;
  before: Partial<AdminClinicianProfile>;
  after: Partial<AdminClinicianProfile>;
  changed: string[];
  result: string;
  note: string;
  created_at: string;
}

export const adminClinicianApi = {
  options: () =>
    request<{ departments: { id: number; name: string }[] }>('/admin/clinicians/options'),

  list: () =>
    request<{ items: AdminClinicianItem[]; total: number }>('/admin/clinicians'),

  get: (clinicianId: number) =>
    request<AdminClinicianDetail>(`/admin/clinicians/${clinicianId}`),

  /** 该医生的档案变更审计（新的在前） */
  audit: (clinicianId: number, limit = 20) =>
    request<{ items: AdminAuditEntry[]; total: number }>(
      `/admin/clinicians/${clinicianId}/audit?limit=${limit}`,
    ),

  /** 医生档案：只传要改的字段；status 只接受 active / inactive */
  updateProfile: (
    clinicianId: number,
    patch: Partial<Omit<AdminClinicianProfile, 'note'>> & { note?: string },
  ) =>
    request<{ clinician_id: number; profile: AdminClinicianProfile }>(
      `/admin/clinicians/${clinicianId}/profile`,
      { method: 'PUT', body: patch },
    ),

  grantRole: (clinicianId: number, note = '') =>
    request<{ granted: boolean; roles: string[] }>(`/admin/clinicians/${clinicianId}/role`, {
      method: 'POST',
      body: { note },
    }),

  revokeRole: (clinicianId: number) =>
    request<{ revoked: boolean; bindings_removed: { departments: number; patients: number } }>(
      `/admin/clinicians/${clinicianId}/role`,
      { method: 'DELETE' },
    ),

  bindDepartment: (clinicianId: number, departmentId: number, isPrimary = false) =>
    request<{ department_id: number; name: string; is_primary: boolean }>(
      `/admin/clinicians/${clinicianId}/departments`,
      { method: 'POST', body: { department_id: departmentId, is_primary: isPrimary } },
    ),

  unbindDepartment: (clinicianId: number, departmentId: number) =>
    request<{ removed: boolean }>(
      `/admin/clinicians/${clinicianId}/departments/${departmentId}`,
      { method: 'DELETE' },
    ),

  bindPatient: (clinicianId: number, patientId: number, relation = 'assigned') =>
    request<{ patient_id: number; name: string; relation: string }>(
      `/admin/clinicians/${clinicianId}/patients`,
      { method: 'POST', body: { patient_id: patientId, relation } },
    ),

  unbindPatient: (clinicianId: number, patientId: number) =>
    request<{ removed: boolean }>(
      `/admin/clinicians/${clinicianId}/patients/${patientId}`,
      { method: 'DELETE' },
    ),
};

// ── C3：人工接管（坐席侧） ────────────────────────────────────────────────
// 医护端「人工坐席」页用：队列 / 会话消息 / 认领 / 回复 / 结单。
// 全部传 authResetOn403: false —— 403 是"你没有 handoff:write scope"这类业务拒绝，
// 不该把医生踢下线（真正的凭证失效是 401）。

export const handoffApi = {
  queue: (params: { status?: string; mine?: boolean; only_active?: boolean; limit?: number } = {}) =>
    request<HandoffQueue>('/handoff/queue', { params, authResetOn403: false }),
  detail: (ticketId: number) =>
    request<{ ticket: HandoffTicket }>(`/handoff/tickets/${ticketId}`, { authResetOn403: false }),

  messages: (ticketId: number, limit = 30) =>
    request<{ items: HandoffSessionMessage[]; total: number }>(
      `/handoff/tickets/${ticketId}/messages`,
      { params: { limit }, authResetOn403: false },
    ),

  claim: (ticketId: number) =>
    request<{ ticket: HandoffTicket }>(`/handoff/tickets/${ticketId}/claim`, {
      method: 'POST',
      authResetOn403: false,
    }),

  reply: (ticketId: number, content: string) =>
    request<{ ticket: HandoffTicket }>(`/handoff/tickets/${ticketId}/reply`, {
      method: 'POST',
      body: { content },
      authResetOn403: false,
    }),

  resolve: (ticketId: number, resolution: string) =>
    request<{ ticket: HandoffTicket; closing: string }>(
      `/handoff/tickets/${ticketId}/resolve`,
      { method: 'POST', body: { resolution }, authResetOn403: false, timeoutMs: 60_000 },
    ),
};

// ── C3：人工接管（患者侧） ───────────────────────────────────────────────
// 患者点「转人工」→ 建单；状态条读 mine；未认领时可撤回。
// 403 同样按业务拒绝处理：游客/非本人不是凭证问题，不该被登出。

export const patientHandoffApi = {
  create: (body: {
    session_id: string;
    reason?: 'patient_request' | 'model_escalate' | 'emergency' | 'unclear';
    priority?: string;
    summary?: string;
  }) =>
    request<{ created: boolean; ticket: HandoffTicket }>('/handoff/tickets', {
      method: 'POST',
      body,
      authResetOn403: false,
    }),

  /** 我的工单；session_id 只在当前会话里筛，active_only=false 可看历史 */
  mine: (params: { session_id?: string; active_only?: boolean } = {}) =>
    request<{ items: HandoffTicket[]; total: number }>('/handoff/tickets/mine', {
      params,
      authResetOn403: false,
    }),

  cancel: (ticketId: number) =>
    request<{ ticket: HandoffTicket; closing: string }>(
      `/handoff/tickets/${ticketId}/cancel`,
      { method: 'POST', authResetOn403: false, timeoutMs: 60_000 },
    ),
};

// ── 管理端：人工接管工单（只读） ──────────────────────────────────────────
// 管理端只做审计：能看列表、详情、坐席和患者说了什么，不能认领/回复
// （权限口径：坐席角色才带 handoff:write）。

export const adminHandoffApi = {
  tickets: (params: { status?: string; only_active?: boolean; limit?: number } = {}) =>
    request<HandoffQueue>('/admin/handoff/tickets', { params }),

  detail: (ticketId: number) =>
    request<{ ticket: HandoffTicket }>(`/admin/handoff/tickets/${ticketId}`),

  messages: (ticketId: number, limit = 30) =>
    request<{ items: HandoffSessionMessage[]; total: number }>(
      `/admin/handoff/tickets/${ticketId}/messages`,
      { params: { limit } },
    ),
};

// ── 管理端：租户（C1） ────────────────────────────────────────────────────
// 管理员账号是全局的，工作租户由请求头 X-Tenant-Code 决定（见 shared client 的
// setTenantReader）；这里只负责给「租户切换器」提供下拉数据。

export interface TenantItem {
  id: number;
  code: string;
  name: string;
  status: 'active' | 'suspended' | string;
  contact_name?: string | null;
  contact_phone?: string | null;
  created_at?: string | null;
}

export const adminTenantApi = {
  list: () => request<{ tenants: TenantItem[]; total: number }>('/admin/tenants'),
};
