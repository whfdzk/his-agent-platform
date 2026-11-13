import { lazy, Suspense } from 'react';
import { createBrowserRouter, Navigate } from 'react-router-dom';

import { AppShell, RequireAuth } from './components/AppShell';
import { HomePage } from './pages/HomePage';

// 路由级懒加载：把 recharts / react-big-calendar / framer-motion 等重依赖
// 从首屏 chunk 里拆出去，首页只加载真正需要的代码。
// AppShell 内已有 <Suspense>，这里的懒组件可以直接使用。
const ChatPage = lazy(() => import('./pages/ChatPage').then((m) => ({ default: m.ChatPage })));
const ReportsPage = lazy(() =>
  import('./pages/ReportsPage').then((m) => ({ default: m.ReportsPage })),
);
const ReportDetailPage = lazy(() =>
  import('./pages/ReportDetailPage').then((m) => ({ default: m.ReportDetailPage })),
);
const ReportComparePage = lazy(() =>
  import('./pages/ReportComparePage').then((m) => ({ default: m.ReportComparePage })),
);
const HealthPage = lazy(() =>
  import('./pages/HealthPage').then((m) => ({ default: m.HealthPage })),
);
const HealthProfilePage = lazy(() =>
  import('./pages/HealthProfilePage').then((m) => ({ default: m.HealthProfilePage })),
);
const MePage = lazy(() => import('./pages/MePage').then((m) => ({ default: m.MePage })));
const HistoryPage = lazy(() =>
  import('./pages/HistoryPage').then((m) => ({ default: m.HistoryPage })),
);
const HistoryDetailPage = lazy(() =>
  import('./pages/HistoryDetailPage').then((m) => ({ default: m.HistoryDetailPage })),
);
const AppointmentCalendarPage = lazy(() =>
  import('./pages/AppointmentCalendarPage').then((m) => ({ default: m.AppointmentCalendarPage })),
);
const LoginPage = lazy(() => import('./pages/LoginPage').then((m) => ({ default: m.LoginPage })));

export const router = createBrowserRouter([
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <HomePage /> },
      // AI 咨询允许游客使用（不落库），其余页面需要登录
      { path: 'consult', element: <ChatPage /> },
      {
        path: 'reports',
        element: (
          <RequireAuth>
            <ReportsPage />
          </RequireAuth>
        ),
      },
      {
        path: 'reports/compare',
        element: (
          <RequireAuth>
            <ReportComparePage />
          </RequireAuth>
        ),
      },
      {
        path: 'reports/:id',
        element: (
          <RequireAuth>
            <ReportDetailPage />
          </RequireAuth>
        ),
      },
      {
        path: 'profile',
        element: (
          <RequireAuth>
            <HealthProfilePage />
          </RequireAuth>
        ),
      },
      {
        path: 'health',
        element: (
          <RequireAuth>
            <HealthPage />
          </RequireAuth>
        ),
      },
      {
        path: 'me',
        element: (
          <RequireAuth>
            <MePage />
          </RequireAuth>
        ),
      },
      {
        path: 'history',
        element: (
          <RequireAuth>
            <HistoryPage />
          </RequireAuth>
        ),
      },
      {
        path: 'history/:sessionId',
        element: (
          <RequireAuth>
            <HistoryDetailPage />
          </RequireAuth>
        ),
      },
      {
        path: 'appointments',
        element: (
          <RequireAuth>
            <AppointmentCalendarPage />
          </RequireAuth>
        ),
      },
    ],
  },
  {
    path: '/login',
    // 登录页不在 AppShell 内，需要自己的 Suspense 边界
    element: (
      <Suspense fallback={<div className="p-6 text-sm text-slate-400">加载中…</div>}>
        <LoginPage />
      </Suspense>
    ),
  },
  { path: '*', element: <Navigate to="/" replace /> },
]);
