import { useEffect, useMemo, useState } from 'react';
import dayjs from 'dayjs';
import { Calendar, dayjsLocalizer } from 'react-big-calendar';
import 'react-big-calendar/lib/css/react-big-calendar.css';
import { CalendarCheck2, Plus, X } from 'lucide-react';
import { appointmentApi, type Appointment } from '@his/shared';
import { useUserSession } from '../store/session';

const localizer = dayjsLocalizer(dayjs);

/** 目录接口返回的可挂号医生（只包含有档案 + 在岗 + 有科室绑定的） */
type DoctorOption = {
  clinician_id: number;
  name: string;
  title: string;
  specialty: string;
  departments: { id: number; name: string; is_primary: boolean }[];
};

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

  // 新建挂号：只让患者选择"目录里真实在岗的医生"，不手填医生姓名。
  // 带上 doctor_clinician_id 之后，后端确认时会自动把这位医生与该患者建立接诊关系。
  const [creating, setCreating] = useState(false);
  const [doctors, setDoctors] = useState<DoctorOption[]>([]);
  const [docId, setDocId] = useState('');
  const [visitDate, setVisitDate] = useState('');
  const [slot, setSlot] = useState('上午');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [createError, setCreateError] = useState('');

  const reload = () => {
    appointmentApi.list()
      .then((r) => setAppointments(r.appointments))
      .catch(() => setAppointments([]));
  };

  useEffect(() => {
    if (!userId) return;
    reload();
  }, [userId]);

  const openCreate = async () => {
    setCreating(true);
    setCreateError('');
    setNotice('');
    setDocId('');
    setSlot('上午');
    setVisitDate(dayjs().add(1, 'day').format('YYYY-MM-DD'));
    try {
      const r = await appointmentApi.doctors();
      // 没有科室绑定的医生挂不进任何科室，直接不列
      setDoctors(r.doctors.filter((d) => d.departments.length > 0));
    } catch (e) {
      setDoctors([]);
      setCreateError((e as Error).message);
    }
  };

  const submitCreate = async () => {
    const doc = doctors.find((d) => d.clinician_id === Number(docId));
    if (!doc) {
      setCreateError('请选择医生');
      return;
    }
    const dept = doc.departments.find((d) => d.is_primary) || doc.departments[0];
    setBusy(true);
    setCreateError('');
    try {
      const r = await appointmentApi.create({
        department: dept.name,
        doctor_name: doc.title ? `${doc.name} ${doc.title}` : doc.name,
        doctor_clinician_id: doc.clinician_id,
        date: visitDate,
        time_slot: slot,
        fee: 20,
        queue_number: 0,
      });
      if (r.appointment) {
        setAppointments((prev) => [...prev, r.appointment]);
      }
      setCreating(false);
      setNotice(`挂号成功：${r.appointment?.appointment_no ?? ''}（${dept.name} · ${doc.name}）`);
    } catch (e) {
      setCreateError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

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
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 text-sm font-semibold tracking-tight text-slate-900">
        <span>我的预约</span>
        <button
          onClick={() => void openCreate()}
          className="flex items-center gap-1 rounded-lg bg-primary-600 px-2.5 py-1.5 text-xs font-normal text-white hover:bg-primary-700"
        >
          <Plus className="h-3.5 w-3.5" />
          新建挂号
        </button>
      </header>

      {notice && (
        <div className="mx-auto w-full max-w-2xl px-3 pt-2 xl:max-w-4xl">
          <div className="rounded-lg border border-emerald-100 bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
            {notice}
          </div>
        </div>
      )}

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

      {/* 新建挂号 Modal */}
      {creating && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-6">
          <div className="w-full max-w-sm rounded-2xl bg-white p-5 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center gap-2 text-base font-medium text-gray-800">
                <Plus className="h-5 w-5 text-primary-600" /> 新建挂号
              </div>
              <button onClick={() => setCreating(false)} aria-label="关闭">
                <X className="h-5 w-5 text-gray-400" />
              </button>
            </div>

            {createError && (
              <div className="mb-2 rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-600">
                {createError}
              </div>
            )}

            <div className="space-y-2 text-sm">
              <label className="block">
                <span className="text-xs text-gray-400">医生</span>
                <select
                  value={docId}
                  onChange={(e) => setDocId(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none"
                >
                  <option value="">{doctors.length ? '选择医生' : '暂无在岗医生'}</option>
                  {doctors.map((d) => {
                    const dept = d.departments.find((x) => x.is_primary) || d.departments[0];
                    return (
                      <option key={d.clinician_id} value={d.clinician_id}>
                        {dept.name} · {d.name}
                        {d.title ? ` ${d.title}` : ''}
                        {d.specialty ? `（${d.specialty}）` : ''}
                      </option>
                    );
                  })}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="block">
                  <span className="text-xs text-gray-400">日期</span>
                  <input
                    type="date"
                    value={visitDate}
                    onChange={(e) => setVisitDate(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none"
                  />
                </label>
                <label className="block">
                  <span className="text-xs text-gray-400">时段</span>
                  <select
                    value={slot}
                    onChange={(e) => setSlot(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none"
                  >
                    <option value="上午">上午</option>
                    <option value="下午">下午</option>
                  </select>
                </label>
              </div>
              <p className="text-[11px] leading-relaxed text-gray-400">
                挂号费按 ¥20 计。确认后这位医生会自动成为你的接诊医生，能看到本次问诊记录。
              </p>
            </div>

            <button
              disabled={busy}
              onClick={() => void submitCreate()}
              className="mt-4 w-full rounded-xl bg-primary-600 py-2 text-sm text-white hover:bg-primary-700 disabled:opacity-50"
            >
              {busy ? '提交中…' : '确认挂号'}
            </button>
          </div>
        </div>
      )}

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
