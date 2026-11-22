"""add admin_audit_logs

管理端写操作审计（C4 续 · 第 ③ 件）。

医生档案 PUT /api/v1/admin/clinicians/{id}/profile 此前只写 loguru：
日志轮转后"谁把某医生改成停诊/改了什么职称"无法回溯。档案 status 直接决定
患者能不能挂到号，因此改为日志照旧 + 落库留痕，并可通过
GET /api/v1/admin/clinicians/{id}/audit 查询。

Revision ID: b7e3c9f0d218
Revises: a9c2d5f1e4b8
Create Date: 2026-11-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b7e3c9f0d218'
down_revision: Union[str, Sequence[str], None] = 'a9c2d5f1e4b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        'admin_audit_logs',
        *_base_columns(),
        sa.Column('actor', sa.String(length=100), server_default='', nullable=False, comment='操作人（admin JWT sub）'),
        sa.Column('actor_name', sa.String(length=100), server_default='', nullable=False, comment='操作人用户名'),
        sa.Column('action', sa.String(length=64), server_default='', nullable=False, comment='动作，如 clinician.profile.update'),
        sa.Column('target_kind', sa.String(length=32), server_default='', nullable=False, comment='对象类型'),
        sa.Column('target_id', sa.String(length=64), server_default='', nullable=False, comment='对象 ID'),
        sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='变更前快照'),
        sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='变更后快照'),
        sa.Column('changed', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='实际变化字段'),
        sa.Column('result', sa.String(length=16), server_default='ok', nullable=False, comment='ok / rejected / failed'),
        sa.Column('note', sa.Text(), server_default='', nullable=False, comment='备注 / 拒绝原因'),
        sa.Column('ip', sa.String(length=64), server_default='', nullable=False, comment='来源 IP'),
        sa.Column('request_id', sa.String(length=64), server_default='', nullable=False, comment='请求 ID'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_admin_audit_actor', 'admin_audit_logs', ['actor'])
    op.create_index('ix_admin_audit_action', 'admin_audit_logs', ['action'])
    op.create_index('ix_admin_audit_target', 'admin_audit_logs', ['target_kind', 'target_id'])


def downgrade() -> None:
    op.drop_index('ix_admin_audit_target', table_name='admin_audit_logs')
    op.drop_index('ix_admin_audit_action', table_name='admin_audit_logs')
    op.drop_index('ix_admin_audit_actor', table_name='admin_audit_logs')
    op.drop_table('admin_audit_logs')
