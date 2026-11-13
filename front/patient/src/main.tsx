import React from 'react';
import ReactDOM from 'react-dom/client';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { UserSessionProvider } from './store/session';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <UserSessionProvider>
      <RouterProvider router={router} />
    </UserSessionProvider>
  </React.StrictMode>,
);
