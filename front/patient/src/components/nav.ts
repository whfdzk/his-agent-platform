import { Activity, FileText, Home, MessageCircle, User } from 'lucide-react';

/** 一级导航（桌面侧栏与移动 TabBar 共用，避免两处维护） */
export const NAV_ITEMS = [
  { to: '/', label: '首页', icon: Home, end: true },
  { to: '/consult', label: 'AI 咨询', icon: MessageCircle },
  { to: '/reports', label: '报告中心', icon: FileText },
  { to: '/health', label: '健康管理', icon: Activity },
  { to: '/me', label: '个人中心', icon: User },
];
