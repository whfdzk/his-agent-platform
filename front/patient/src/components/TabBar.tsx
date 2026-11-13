import { NavLink } from 'react-router-dom';
import clsx from 'clsx';

import { NAV_ITEMS } from './nav';

/** 移动端底部导航：5 个一级入口 */
export function TabBar() {
  return (
    <nav className="safe-bottom fixed inset-x-0 bottom-0 z-30 flex border-t border-slate-200 bg-white/95 backdrop-blur md:hidden">
      {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            clsx(
              'relative flex min-h-14 flex-1 flex-col items-center gap-0.5 py-2 text-[10px] transition-colors',
              isActive ? 'font-medium text-primary-700' : 'text-slate-400 hover:text-slate-600',
            )
          }
        >
          {({ isActive }) => (
            <>
              <Icon className={clsx('h-[20px] w-[20px]', isActive && 'scale-105')} />
              {label}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}
