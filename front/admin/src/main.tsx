import React from 'react';
import ReactDOM from 'react-dom/client';
import { RouterProvider } from 'react-router-dom';
import { ADMIN_TENANT_KEY, ADMIN_TOKEN_KEY, setTenantReader, setTokenReader } from '@his/shared';
import { router } from './router';
import { AdminSessionProvider } from './store/adminSession';
import './index.css';

// 管理端用独立 token key：@his/shared 里所有 request 自动带上管理员凭证，
// 且不会与同一浏览器上患者端的 his_token 互相顶掉。
setTokenReader(() => localStorage.getItem(ADMIN_TOKEN_KEY) || undefined);

// 工作租户：管理端切换器写入 localStorage，这里统一给所有请求注入 X-Tenant-Code。
// 未设置时返回 undefined，后端回落到 token 里的 tid（管理员默认租户）。
setTenantReader(() => localStorage.getItem(ADMIN_TENANT_KEY) || undefined);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AdminSessionProvider>
      <RouterProvider router={router} />
    </AdminSessionProvider>
  </React.StrictMode>,
);
