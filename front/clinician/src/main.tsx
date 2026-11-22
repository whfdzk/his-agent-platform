import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { setTokenReader } from '@his/shared';

import { AppRouter } from './router';
import { ClinicianSessionProvider } from './store/session';
import './index.css';

/**
 * 医护端用独立 token key：同一台机器同时开着患者端 / 管理端时不互相顶掉。
 * shared 里所有 request 都从这个 key 取凭证。
 */
setTokenReader(() => localStorage.getItem('his_clinician_token') || undefined);

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ClinicianSessionProvider>
        <AppRouter />
      </ClinicianSessionProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
