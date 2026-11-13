import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, Pencil, Save, ShieldCheck, UserRound, X } from 'lucide-react';
import { patientApi } from '@his/shared';
import type { HealthProfile, ProfileUpdate } from '@his/shared';

import { Button, Card, Field, Ring, SectionHeader, Skeleton, Tag, inputCls } from '../components/ui';

type FieldType = 'text' | 'number' | 'date' | 'select' | 'textarea';

interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  options?: string[];
  placeholder?: string;
  span?: 1 | 2;
}

const BASIC_FIELDS: FieldDef[] = [
  { key: 'name', label: '姓名', type: 'text' },
  { key: 'gender', label: '性别', type: 'select', options: ['男', '女'] },
  { key: 'age', label: '年龄', type: 'number' },
  { key: 'birth_date', label: '出生日期', type: 'date' },
  { key: 'blood_type', label: '血型', type: 'select', options: ['A', 'B', 'O', 'AB', '未知'] },
  { key: 'height_cm', label: '身高（cm）', type: 'number' },
  { key: 'weight_kg', label: '体重（kg）', type: 'number' },
  { key: 'waist_cm', label: '腰围（cm）', type: 'number' },
  { key: 'marital_status', label: '婚姻状况', type: 'select', options: ['未婚', '已婚', '其他'] },
  { key: 'occupation', label: '职业', type: 'text' },
  { key: 'city', label: '常住城市', type: 'text' },
  { key: 'emergency_contact_name', label: '紧急联系人', type: 'text' },
  { key: 'emergency_contact_phone', label: '紧急联系人电话', type: 'text', placeholder: '如需更新请重新填写' },
];

const LIFE_FIELDS: FieldDef[] = [
  {
    key: 'smoking',
    label: '吸烟',
    type: 'select',
    options: ['never', 'former', 'current'],
  },
  {
    key: 'drinking',
    label: '饮酒',
    type: 'select',
    options: ['never', 'occasional', 'regular'],
  },
];

const HISTORY_FIELDS: FieldDef[] = [
  { key: 'allergy_history', label: '过敏史', type: 'textarea', span: 2, placeholder: '如：青霉素过敏、海鲜过敏' },
  { key: 'medical_history', label: '既往病史', type: 'textarea', span: 2, placeholder: '如：高血压 5 年，规律服药' },
  { key: 'surgery_history', label: '手术史', type: 'textarea', span: 2, placeholder: '如：2019 年阑尾切除术' },
  { key: 'family_history', label: '家族史', type: 'textarea', span: 2, placeholder: '如：父亲糖尿病、母亲高血压' },
  { key: 'chronic_diseases', label: '慢性病标签', type: 'textarea', span: 2, placeholder: '如：高血压,2型糖尿病' },
];

const SELECT_LABELS: Record<string, Record<string, string>> = {
  smoking: { never: '从不', former: '已戒烟', current: '目前吸烟' },
  drinking: { never: '从不', occasional: '偶尔', regular: '经常' },
};

const ALL_FIELDS = [...BASIC_FIELDS, ...LIFE_FIELDS, ...HISTORY_FIELDS];
const NUMERIC_KEYS = new Set(['age', 'height_cm', 'weight_kg', 'waist_cm']);

function toForm(profile: HealthProfile): Record<string, string> {
  const form: Record<string, string> = {};
  ALL_FIELDS.forEach((f) => {
    const raw = (profile as unknown as Record<string, unknown>)[f.key];
    form[f.key] = raw === null || raw === undefined ? '' : String(raw);
  });
  // 紧急联系人电话后端只回掩码，编辑框留空避免把掩码写回
  form.emergency_contact_phone = '';
  return form;
}

export function HealthProfilePage() {
  const navigate = useNavigate();
  const [profile, setProfile] = useState<HealthProfile | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await patientApi.profile();
      setProfile(res);
      setForm(toForm(res));
    } catch (e) {
      setError((e as Error).message || '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const bmiHint = useMemo(() => {
    if (!profile?.bmi) return '';
    const map: Record<string, string> = {
      low: '偏瘦',
      normal: '正常',
      high: '超重',
      critical: '肥胖',
    };
    return `${profile.bmi} · ${map[profile.bmi_level] || '—'}`;
  }, [profile]);

  const save = async () => {
    setSaving(true);
    setError('');
    try {
      const payload: ProfileUpdate = {};
      ALL_FIELDS.forEach((f) => {
        const value = form[f.key];
        if (f.key === 'emergency_contact_phone' && !value) return;
        if (value === '') {
          (payload as Record<string, unknown>)[f.key] = null;
          return;
        }
        (payload as Record<string, unknown>)[f.key] = NUMERIC_KEYS.has(f.key) ? Number(value) : value;
      });
      await patientApi.updateProfile(payload);
      setEditing(false);
      await load();
    } catch (e) {
      setError((e as Error).message || '保存失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="mx-auto w-full max-w-3xl space-y-3 px-4 pt-5 2xl:max-w-5xl">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="h-32" />
        <Skeleton className="h-48" />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-3xl px-4 pb-24 pt-4 md:pb-10 2xl:max-w-5xl">
        <div className="mb-3 flex items-center justify-between">
          <button
            onClick={() => navigate(-1)}
            className="flex h-9 items-center gap-1 rounded-lg px-1 text-sm text-slate-500 hover:text-slate-700"
          >
            <ArrowLeft className="h-4 w-4" /> 返回
          </button>
          {editing ? (
            <div className="flex gap-1.5">
              <Button size="sm" variant="secondary" onClick={() => setEditing(false)} disabled={saving}>
                <X className="h-3.5 w-3.5" /> 取消
              </Button>
              <Button size="sm" onClick={save} disabled={saving}>
                <Save className="h-3.5 w-3.5" /> {saving ? '保存中' : '保存'}
              </Button>
            </div>
          ) : (
            <Button size="sm" variant="secondary" onClick={() => setEditing(true)}>
              <Pencil className="h-3.5 w-3.5" /> 编辑
            </Button>
          )}
        </div>

        <header className="mb-4 flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-primary-50 text-primary-700">
            <UserRound className="h-5 w-5" />
          </div>
          <div className="flex-1">
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">健康档案</h1>
            <p className="mt-0.5 text-xs text-slate-400">完整的档案能让 AI 建议更准确</p>
          </div>
          {profile && <Ring percent={profile.completeness} size={64} label="完整度" />}
        </header>

        {error && <div className="mb-3 rounded-xl bg-red-50 px-3 py-2 text-xs text-red-500">{error}</div>}

        {!profile ? (
          <Card className="p-6 text-center text-sm text-slate-500">档案加载失败</Card>
        ) : (
          <>
            {/* 基本资料 */}
            <SectionHeader title="基本资料" />
            <Card className="mb-4 p-4">
              {editing ? (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {BASIC_FIELDS.map((f) => (
                    <Field key={f.key} label={f.label} className={f.span === 2 ? 'sm:col-span-2' : ''}>
                      <FieldInput
                        def={f}
                        value={form[f.key] || ''}
                        onChange={(v) => setForm({ ...form, [f.key]: v })}
                      />
                    </Field>
                  ))}
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
                  <Info label="姓名" value={profile.name} />
                  <Info label="性别" value={profile.gender || '未填写'} />
                  <Info label="年龄" value={profile.age ? `${profile.age} 岁` : '未填写'} />
                  <Info label="出生日期" value={profile.birth_date || '未填写'} />
                  <Info label="血型" value={profile.blood_type || '未填写'} />
                  <Info label="手机号" value={profile.phone_masked || '未绑定'} />
                  <Info label="身高" value={profile.height_cm ? `${profile.height_cm} cm` : '未填写'} />
                  <Info label="体重" value={profile.weight_kg ? `${profile.weight_kg} kg` : '未填写'} />
                  <Info label="BMI" value={bmiHint || '未填写'} />
                  <Info label="腰围" value={profile.waist_cm ? `${profile.waist_cm} cm` : '未填写'} />
                  <Info label="婚姻" value={profile.marital_status || '未填写'} />
                  <Info label="职业" value={profile.occupation || '未填写'} />
                  <Info label="城市" value={profile.city || '未填写'} />
                  <Info label="紧急联系人" value={profile.emergency_contact_name || '未填写'} />
                  <Info
                    label="联系人电话"
                    value={profile.emergency_contact_phone_masked || '未填写'}
                  />
                </div>
              )}
            </Card>

            {/* 生活方式 */}
            <SectionHeader title="生活方式" />
            <Card className="mb-4 p-4">
              {editing ? (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  {LIFE_FIELDS.map((f) => (
                    <Field key={f.key} label={f.label}>
                      <FieldInput
                        def={f}
                        value={form[f.key] || ''}
                        onChange={(v) => setForm({ ...form, [f.key]: v })}
                        labels={SELECT_LABELS[f.key]}
                      />
                    </Field>
                  ))}
                </div>
              ) : (
                <div className="flex flex-wrap gap-2">
                  <Tag tone="slate">
                    吸烟：{SELECT_LABELS.smoking[profile.smoking || ''] || '未填写'}
                  </Tag>
                  <Tag tone="slate">
                    饮酒：{SELECT_LABELS.drinking[profile.drinking || ''] || '未填写'}
                  </Tag>
                </div>
              )}
            </Card>

            {/* 病史 */}
            <SectionHeader title="病史与过敏" subtitle="AI 会据此规避用药与检查风险" />
            <Card className="mb-4 divide-y divide-slate-100">
              {editing ? (
                <div className="grid grid-cols-1 gap-3 p-4">
                  {HISTORY_FIELDS.map((f) => (
                    <Field key={f.key} label={f.label}>
                      <FieldInput
                        def={f}
                        value={form[f.key] || ''}
                        onChange={(v) => setForm({ ...form, [f.key]: v })}
                      />
                    </Field>
                  ))}
                </div>
              ) : (
                HISTORY_FIELDS.map((f) => (
                  <div key={f.key} className="px-4 py-3">
                    <div className="text-[11px] text-slate-400">{f.label}</div>
                    <div className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-slate-700">
                      {(profile as unknown as Record<string, string | null>)[f.key] || '未填写'}
                    </div>
                  </div>
                ))
              )}
            </Card>

            <Card className="flex items-center gap-3 px-4 py-3">
              <ShieldCheck className="h-4 w-4 shrink-0 text-primary-600" />
              <div className="min-w-0 flex-1 text-[11px] leading-relaxed text-slate-500">
                档案仅用于本人健康管理与问诊参考，不对外共享；可在个人中心管理授权与导出/删除数据。
              </div>
              <button onClick={() => navigate('/me')} className="shrink-0 text-[11px] text-primary-700">
                隐私设置
              </button>
            </Card>
          </>
        )}
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] text-slate-400">{label}</div>
      <div className="mt-0.5 truncate text-[13px] text-slate-700">{value}</div>
    </div>
  );
}

function FieldInput({
  def,
  value,
  onChange,
  labels,
}: {
  def: FieldDef;
  value: string;
  onChange: (v: string) => void;
  labels?: Record<string, string>;
}) {
  if (def.type === 'select') {
    return (
      <select className={inputCls} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">未填写</option>
        {(def.options || []).map((o) => (
          <option key={o} value={o}>
            {labels?.[o] || o}
          </option>
        ))}
      </select>
    );
  }
  if (def.type === 'textarea') {
    return (
      <textarea
        className={`${inputCls} min-h-[72px] resize-y`}
        value={value}
        placeholder={def.placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <input
      className={inputCls}
      type={def.type === 'number' ? 'number' : def.type === 'date' ? 'date' : 'text'}
      value={value}
      placeholder={def.placeholder}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
