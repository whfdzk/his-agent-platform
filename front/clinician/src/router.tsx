import type { ReactElement } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import { useClinicianSession } from './store/session';
import { WorkbenchLayout } from './components/WorkbenchLayout';
import { AlertsPage } from './pages/AlertsPage';
import { HandoffPage } from './pages/HandoffPage';
import { LoginPage } from './pages/LoginPage';
import { PatientsPage } from './pages/PatientsPage';
import { QueuePage } from './pages/QueuePage';

function RequireClinician({ children }: { children: ReactElement }) {
  const { token, ready } = useClinicianSession();
  const location = useLocation();

  // 恢复本地登录态期间不要闪一下登录页
  if (!ready) {
    return (
      <div className="flex min-h-dvh items-center justify-center text-xs text-slate-400">加载中…</div>
    );
  }
  if (!token) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return children;
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <RequireClinician>
            <WorkbenchLayout>
              <QueuePage />
            </WorkbenchLayout>
          </RequireClinician>
        }
      />
      <Route
        path="/queue"
        element={
          <RequireClinician>
            <WorkbenchLayout>
              <QueuePage />
            </WorkbenchLayout>
          </RequireClinician>
        }
      />
      <Route
        path="/alerts"
        element={
          <RequireClinician>
            <WorkbenchLayout>
              <AlertsPage />
            </WorkbenchLayout>
          </RequireClinician>
        }
      />
      <Route
        path="/patients"
        element={
          <RequireClinician>
            <WorkbenchLayout>
              <PatientsPage />
            </WorkbenchLayout>
          </RequireClinician>
        }
      />
      <Route
        path="/handoff"
        element={
          <RequireClinician>
            <WorkbenchLayout>
              <HandoffPage />
            </WorkbenchLayout>
          </RequireClinician>
        }
      />
      <Route path="*" element={<Navigate to="/queue" replace />} />
    </Routes>
  );
}
