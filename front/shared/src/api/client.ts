// 统一 fetch 封装：baseURL /api/v1，dev 由 Vite proxy 转发到后端
// 后端已强制 JWT 鉴权：自动注入 localStorage 中的 his_token；401 统一清态并通知应用层

const BASE = '/api/v1';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
  params?: Record<string, string | number | boolean | undefined>;
  headers?: Record<string, string>;
  /** 页面卸载/切后台时仍希望请求送达（如登出） */
  keepalive?: boolean;
  /** 调用方自己的取消信号（如组件卸载） */
  signal?: AbortSignal;
  /** 超时毫秒数，默认 15s：后端挂起时不能让前端永久转圈 */
  timeoutMs?: number;
  /**
   * 403 时是否清登录态，默认 true（401 一律清）。
   * 医护端要把 403 当作业务拒绝（"该患者不在你的责任范围内"），
   * 若沿用"403 就登出"的默认，医生代批一次范围外患者就会被踢出登录。
   */
  authResetOn403?: boolean;
}

export const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;

const TOKEN_KEY = 'his_token';
const USER_KEY = 'his_user';
/** 管理端用独立 key：同一浏览器同时开着患者端时不互相顶掉 */
export const ADMIN_TOKEN_KEY = 'his_admin_token';
export const ADMIN_USER_KEY = 'his_admin_user';
/** 管理端当前工作租户：切换后所有请求带 X-Tenant-Code */
export const ADMIN_TENANT_KEY = 'his_admin_tenant';

/** 本地存储可能不可用（隐私模式 / 被策略禁用），清理不能因此抛错 */
function safeRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/** 只清患者端登录态；匿名 UUID（his_user_id）由 session store 决定是否重置 */
export function clearPatientSession(): void {
  safeRemove(TOKEN_KEY);
  safeRemove(USER_KEY);
}

/** 只清管理端登录态 */
export function clearAdminSession(): void {
  safeRemove(ADMIN_TOKEN_KEY);
  safeRemove(ADMIN_USER_KEY);
  safeRemove(ADMIN_TENANT_KEY);
}

/** 清空两端登录态：token 失效（401）时使用，避免同浏览器另一端拿着废凭证 */
export function clearStoredSession(): void {
  clearPatientSession();
  clearAdminSession();
}

export function getStoredToken(): string | undefined {
  return localStorage.getItem(TOKEN_KEY) || undefined;
}

/**
 * 覆盖取 token 的来源。管理端启动时会切到 his_admin_token，
 * 这样 @his/shared 里所有 request 都自动带上管理员凭证。
 */
let tokenReader: () => string | undefined = () => localStorage.getItem(TOKEN_KEY) || undefined;

export function setTokenReader(fn: () => string | undefined): void {
  tokenReader = fn;
}

/**
 * 覆盖取租户的来源。后端 get_current_admin 用 X-Tenant-Code 决定工作租户，
 * 缺省时回落到 token 里的 tid（管理员账号默认租户）。
 */
let tenantReader: () => string | undefined = () => undefined;

export function setTenantReader(fn: () => string | undefined): void {
  tenantReader = fn;
}

export function getStoredTenant(): string | undefined {
  return localStorage.getItem(ADMIN_TENANT_KEY) || undefined;
}

/**
 * 401 统一处理：清本地凭证 + 通知应用层同步内存态。
 * 只清 storage 时 React 仍认为已登录，页面会继续渲染空数据而不是跳登录页。
 */
function handleUnauthorized(): void {
  clearStoredSession();
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('his:unauthorized'));
  }
}

async function readErrorDetail(resp: Response): Promise<string> {
  let detail = `HTTP ${resp.status}`;
  try {
    const data = await resp.json();
    detail = data.detail || data.message || detail;
  } catch {
    /* ignore */
  }
  return detail;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, params, headers, keepalive, signal, timeoutMs, authResetOn403 } = options;

  const url = new URL(BASE + path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== '') url.searchParams.set(k, String(v));
    }
  }

  const token = tokenReader();
  const tenant = tenantReader();
  const timeout = timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  // 调用方的取消信号与超时信号合并：任一触发都中断请求
  const onAbort = () => controller.abort();
  signal?.addEventListener('abort', onAbort);
  if (signal?.aborted) controller.abort();

  let resp: Response;
  try {
    resp = await fetch(url.toString(), {
      method,
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(tenant ? { 'X-Tenant-Code': tenant } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      keepalive,
      signal: controller.signal,
    });
  } catch (e) {
    if ((e as Error).name === 'AbortError') {
      // 超时与主动取消都归到这一类：给调用方一个可识别的稳定文案
      throw new ApiError(0, signal?.aborted ? '请求已取消' : '请求超时，请检查网络后重试');
    }
    throw e;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
  }

  if (!resp.ok) {
    const detail = await readErrorDetail(resp);
    // 401=凭证失效，403=权限被收回（如管理员被停用）：两者都必须清态，
    // 否则页面会停留在"看起来已登录但所有数据为空"的假登录状态。
    // 医护端等把 403 当业务拒绝的场景可显式传 authResetOn403: false。
    if (resp.status === 401 || (resp.status === 403 && authResetOn403 !== false)) handleUnauthorized();
    throw new ApiError(resp.status, detail);
  }

  // 204（如登出）没有响应体，直接 json() 会抛 SyntaxError
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

/**
 * multipart 上传（报告文件等）。
 * 不能复用 request()：它强制 Content-Type: application/json，会破坏 boundary。
 */
export async function uploadForm<T>(path: string, form: FormData): Promise<T> {
  const token = tokenReader();
  const tenant = tenantReader();
  const resp = await fetch(BASE + path, {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(tenant ? { 'X-Tenant-Code': tenant } : {}),
    },
    body: form,
  });
  if (!resp.ok) {
    const detail = await readErrorDetail(resp);
    // 与管理端/患者端共用同一套失效处理，避免上传接口 401 时页面停留在"假登录"
    if (resp.status === 401) handleUnauthorized();
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}
