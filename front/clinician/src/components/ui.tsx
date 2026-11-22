import clsx from 'clsx';
import { X } from 'lucide-react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';

import type { ClinicianAlertLevel } from '@his/shared';

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('card', className)}>{children}</div>;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
  size?: 'sm' | 'md';
};

const VARIANTS: Record<string, string> = {
  primary: 'bg-primary-700 text-white hover:bg-primary-800 disabled:bg-slate-300',
  secondary: 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50',
  ghost: 'text-slate-500 hover:bg-slate-100 hover:text-slate-700',
  danger: 'bg-red-50 text-red-600 hover:bg-red-100',
};

export function Button({ variant = 'primary', size = 'md', className, children, ...rest }: ButtonProps) {
  return (
    <button
      className={clsx(
        'inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/30 disabled:cursor-not-allowed disabled:opacity-60',
        size === 'sm' ? 'px-3 py-1.5 text-xs' : 'px-4 py-2 text-sm',
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

const LEVEL_TONE: Record<ClinicianAlertLevel, string> = {
  red: 'bg-red-500 text-white',
  orange: 'bg-orange-500 text-white',
  yellow: 'bg-amber-100 text-amber-700',
  green: 'bg-slate-100 text-slate-500',
};

/** 等级徽标：颜色必须跟 emergency_reply 的处置口径一致，不另造一套色 */
export function LevelBadge({ level, label }: { level: ClinicianAlertLevel; label?: string }) {
  const text =
    label ||
    ({ red: '危急', orange: '紧急', yellow: '次紧急', green: '常规' } as Record<ClinicianAlertLevel, string>)[level];
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-md px-2 py-0.5 text-[11px] font-semibold tracking-wide',
        LEVEL_TONE[level] || LEVEL_TONE.green,
      )}
    >
      {text}
    </span>
  );
}

export function Tag({
  children,
  tone = 'slate',
}: {
  children: ReactNode;
  tone?: 'slate' | 'blue' | 'emerald' | 'amber' | 'red' | 'violet';
}) {
  const tones: Record<string, string> = {
    slate: 'bg-slate-100 text-slate-600',
    blue: 'bg-primary-50 text-primary-700',
    emerald: 'bg-emerald-50 text-emerald-600',
    amber: 'bg-amber-50 text-amber-600',
    red: 'bg-red-50 text-red-600',
    violet: 'bg-violet-50 text-violet-600',
  };
  return (
    <span className={clsx('inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium', tones[tone])}>
      {children}
    </span>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      {icon && (
        <div className="mb-1 flex h-12 w-12 items-center justify-center rounded-xl border border-slate-100 bg-slate-50 text-slate-300">
          {icon}
        </div>
      )}
      <div className="text-sm font-medium text-slate-700">{title}</div>
      {description && <p className="max-w-sm text-xs leading-relaxed text-slate-400">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function InlineError({ text }: { text: string }) {
  if (!text) return null;
  return (
    <div role="alert" className="rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-relaxed text-red-700">
      {text}
    </div>
  );
}

export function Modal({
  open,
  title,
  onClose,
  children,
  footer,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-slate-900/35 backdrop-blur-[2px]" onClick={onClose} />
      <div className="relative z-10 w-full max-w-lg rounded-xl bg-white p-5 shadow-card">
        <div className="mb-3 flex items-start justify-between gap-3">
          <h3 className="text-base font-semibold text-slate-900">{title}</h3>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-100"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
        {footer && <div className="mt-5 flex justify-end gap-2">{footer}</div>}
      </div>
    </div>
  );
}
