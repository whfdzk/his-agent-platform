import { Navigate, Outlet, useLocation } from 'react-router-dom';

import { useAdminSession } from '../store/adminSession';

/**
 * 管理端路由守卫：没有管理员 token 一律踢回 /login。
 *
 * 前端守卫只是体验层（避免先渲染再跳走的闪屏），真正的权限边界在后端
 * —— 各管理端接口都挂了 Depends(get_current_admin)，患者 token 直接 403。
 *
 * token 失效由两处兜底：
 * - 启动时 adminSession 调 /admin/auth/me 校验，失效即清态；
 * - 运行期任意接口 401/403 触发 his:unauthorized，清态后这里立即重定向。
 */
export function RequireAdmin() {
  const { token, admin, ready } = useAdminSession();
  const location = useLocation();

  // 首帧还没从 localStorage 恢复，先不渲染也不跳转
  if (!ready) return null;

  // token 与身份缺一不可：只有 token 没有身份时无法渲染侧边栏，按未登录处理
  if (!token || !admin) {
    return (
      <Navigate
        to="/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }
  return <Outlet />;
}
