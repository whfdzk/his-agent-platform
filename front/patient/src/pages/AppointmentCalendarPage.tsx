import { useEffect, useMemo, useState } from 'react';
import dayjs from 'dayjs';
import { Calendar, dayjsLocalizer } from 'react-big-calendar';
import 'react-big-calendar/lib/css/react-big-calendar.css';
import { CalendarCheck2, X } from 'lucide-react';
import { appointmentApi, type Appointment } from '@his/shared';
import { useUserSession } from '../store/session';

const localizer = dayjsLocalizer(dayjs);

/**
 * 解析预约时段：后端既有 "08:00-12:00" 也有 AI 生成的 "上午" 这类中文时段。
 * 直接按 "上午" 拼时间会让 dayjs 解析出 Invalid Date，日历上整条预约消失。
 */
function slotRange(slot: string | null | undefined): { start: string; end: string } {
  const text = (slot || '').trim();
  const m = text.match(/(\d{1,2}):(\d{2})\s*[-~至]\s*(\d{1,2}):(\d{2})/);
  const pad = (v: string) => v.padStart(2, '0');
  if (m) return { start: `${pad(m[1])}:${m[2]}`, end: `${pad(m[3])}:${m[4]}` };
  if (text.includes('上午')) return { start: '08:00', end: '12:00' };
  if (text.includes('下午')) return { start: '14:00', end: '17:30' };
  if (text.includes('晚')) return { start: '18:00', end: '21:00' };
  const single = text.match(/^(\d{1,2}):(\d{2})$/);
  if (single) {
    const startHour = Number(single[1]);
    return {
      start: `${pad(single[1])}:${single[2]}`,
      end: `${pad(String(Math.min(startHour + 1, 23)))}:${single[2]}`,
    };
  }
  return { start: '09:00', end: '10:00' };
}

export function AppointmentCalendarPage() {
  const { userId } = useUserSession();
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [selected, setSelected] = useState<Appointment | null>(null);

  useEffect(() => {
    if (!userId) return;
    appointmentApi.list()
      .then((r) => setAppointments(r.appointments))
      .catch(() => setAppointments([]));
  }, [userId]);

  const events = useMemo(
    () =>
      appointments
        .filter((a) => a.status === 'confirmed' && a.date)
        .map((a) => {
          const range = slotRange(a.time_slot);
          const start = dayjs(`${a.date} ${range.start}`);
          const end = dayjs(`${a.date} ${range.end}`);
          return {
            title: `${a.department} · ${a.doctor_name || '医生待定'}`,
            start: (start.isValid() ? start : dayjs(a.date)).toDate(),
            end: (end.isValid() ? end : dayjs(a.date).add(1, 'hour')).toDate(),
            resource: a,
          };
        })
        .filter((e) => !Number.isNaN(e.start.getTime())),
    [appointments],
  );

  return (
    <div className="flex h-full flex-col bg-slate-100">
      <header className="border-b border-slate-200 bg-white px-4 py-3 text-sm font-semibold tracking-tight text-slate-900">
        我的预约
      </header>

      <div className="mx-auto w-full max-w-2xl flex-1 overflow-hidden p-2 sm:p-3 xl:max-w-4xl">
        <div className="h-full overflow-hidden rounded-xl border border-slate-200 bg-white p-2">
          <Calendar
            localizer={localizer}
            events={events}
            defaultView="month"
            views={['month', 'agenda']}
            style={{ height: '100%' }}
            onSelectEvent={(e) => setSelected((e as { resource: Appointment }).resource)}
            popup
          />
        </div>
      </div>

      {/* 预约详情 Modal */}
      {selected && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6"
          onClick={() => setSelected(null)}
        >
          <div
            className="w-full max-w-sm rounded-2xl bg-white p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2 text-base font-medium text-gray-800">
                <CalendarCheck2 className="h-5 w-5 text-emerald-600" /> 预约详情
              </div>
              <button onClick={() => setSelected(null)} aria-label="关闭">
                <X className="h-5 w-5 text-gray-400" />
              </button>
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
              <span className="text-gray-400">预约号</span>
              <span className="font-mono">{selected.appointment_no}</span>
              <span className="text-gray-400">科室</span><span>{selected.department}</span>
              <span className="text-gray-400">医生</span><span>{selected.doctor_name}</span>
              <span className="text-gray-400">日期</span><span>{selected.date}</span>
              <span className="text-gray-400">时段</span><span>{selected.time_slot}</span>
              <span className="text-gray-400">序号</span><span>第 {selected.queue_number} 号</span>
              <span className="text-gray-400">费用</span><span>¥{selected.fee}</span>
              <span className="text-gray-400">状态</span>
              <span className={selected.status === 'confirmed' ? 'text-emerald-600' : 'text-gray-400'}>
                {selected.status === 'confirmed' ? '已确认' : '已取消'}
              </span>
            </div>
            {selected.status === 'confirmed' && (
              <button
                onClick={async () => {
                  await appointmentApi.cancel(selected.id).catch(() => undefined);
                  setAppointments((prev) =>
                    prev.map((a) => (a.id === selected.id ? { ...a, status: 'cancelled' } : a)),
                  );
                  setSelected(null);
                }}
                className="mt-4 w-full rounded-xl border border-red-100 bg-red-50 py-2 text-sm text-red-600 hover:bg-red-100"
              >
                取消预约
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
