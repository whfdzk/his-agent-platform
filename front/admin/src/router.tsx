import { createBrowserRouter, Navigate } from 'react-router-dom';
import { AdminLayout } from './components/AdminLayout';
import { RequireAdmin } from './components/RequireAdmin';
import { AdminLoginPage } from './pages/AdminLoginPage';
import { DashboardPage } from './pages/DashboardPage';
import { TokenUsagePage } from './pages/TokenUsagePage';
import { KnowledgePage } from './pages/KnowledgePage';
import { ModelConfigsPage } from './pages/ModelConfigsPage';
import { TracesPage } from './pages/TracesPage';
import { SafetyPage } from './pages/SafetyPage';
import { UsersPage } from './pages/UsersPage';
import { AgentsPage } from './pages/AgentsPage';
import { PromptsPage } from './pages/PromptsPage';
import { ConversationsPage } from './pages/ConversationsPage';
import { SystemConfigPage } from './pages/SystemConfigPage';
import { MembersPage } from './pages/MembersPage';

export const router = createBrowserRouter([
  { path: '/login', element: <AdminLoginPage /> },
  {
    path: '/',
    element: <RequireAdmin />,
    children: [
      {
        element: <AdminLayout />,
        children: [
          { index: true, element: <DashboardPage /> },
          // P0 核心管理
          { path: 'users', element: <UsersPage /> },
          { path: 'agents', element: <AgentsPage /> },
          { path: 'prompts', element: <PromptsPage /> },
          { path: 'models', element: <ModelConfigsPage /> },
          { path: 'knowledge', element: <KnowledgePage /> },
          { path: 'conversations', element: <ConversationsPage /> },
          { path: 'system', element: <SystemConfigPage /> },
          // 审计与扩展
          { path: 'safety', element: <SafetyPage /> },
          { path: 'traces', element: <TracesPage /> },
          // P1 / P2
          { path: 'tokens', element: <TokenUsagePage /> },
          { path: 'members', element: <MembersPage /> },
        ],
      },
    ],
  },
  { path: '*', element: <Navigate to="/" replace /> },
]);
