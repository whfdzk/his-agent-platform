"""下线自研链路追踪：删除 agent_traces / agent_spans

Revision ID: a7b3c9d1e5f2
Revises: f3a9c1d7b5e2
Create Date: 2026-11-23

背景：链路追踪整体切到 Langfuse 后，`src/core/tracing.py` 与
`src/modules/ops/trace_model.py` 已删除，`persist_writer` 也不再写这两张表。
表留着会让「两套方案」的假象一直在：有人看到表就以为查询还得走 PG。
这里把表、序列、索引一并删掉，历史数据已备份到
`backups/agent-trace-tables-20261123.sql`（1118 条 trace / 9551 条 span）。

downgrade 按原 pg_dump schema 原样重建（空表），仅用于必要时回滚结构，
历史数据需从备份文件恢复。
"""

from __future__ import annotations

from alembic import op

revision = "a7b3c9d1e5f2"
down_revision = "f3a9c1d7b5e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS agent_spans")
    op.execute("DROP TABLE IF EXISTS agent_traces")
    op.execute("DROP SEQUENCE IF EXISTS agent_spans_id_seq")
    op.execute("DROP SEQUENCE IF EXISTS agent_traces_id_seq")


def downgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE agent_traces_id_seq
            START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1
        """
    )
    op.execute(
        """
        CREATE TABLE agent_traces (
            id bigint NOT NULL DEFAULT nextval('agent_traces_id_seq'::regclass),
            created_at timestamp with time zone DEFAULT now(),
            updated_at timestamp with time zone DEFAULT now(),
            trace_id character varying(32) NOT NULL,
            request_id character varying(32),
            user_id character varying(64),
            session_id character varying(100),
            turn_id character varying(64),
            entrypoint character varying(100),
            agent_name character varying(64),
            agent_version character varying(32),
            prompt_version character varying(64),
            model_name character varying(100),
            model_version character varying(100),
            status character varying(20),
            error_type character varying(64),
            latency_ms integer,
            prompt_tokens integer,
            completion_tokens integer,
            total_tokens integer,
            tool_calls integer,
            tool_names jsonb,
            agent_calls integer,
            retrieval_count integer,
            retrieval_sources jsonb,
            safety_level character varying(16),
            safety_result character varying(20),
            safety_rules jsonb,
            input_chars integer,
            output_chars integer,
            tenant_id bigint DEFAULT '1'::bigint NOT NULL
        )
        """
    )
    op.execute("ALTER SEQUENCE agent_traces_id_seq OWNED BY agent_traces.id")
    op.execute("ALTER TABLE agent_traces ADD CONSTRAINT agent_traces_pkey PRIMARY KEY (id)")
    op.execute("CREATE INDEX ix_agent_traces_created ON agent_traces USING btree (created_at)")
    op.execute("CREATE INDEX ix_agent_traces_session ON agent_traces USING btree (session_id)")
    op.execute("CREATE INDEX ix_agent_traces_tenant ON agent_traces USING btree (tenant_id)")
    op.execute(
        "CREATE UNIQUE INDEX ix_agent_traces_trace_id ON agent_traces USING btree (trace_id)"
    )
    op.execute(
        "CREATE INDEX ix_agent_traces_user_time ON agent_traces USING btree (user_id, created_at)"
    )

    op.execute(
        """
        CREATE SEQUENCE agent_spans_id_seq
            START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1
        """
    )
    op.execute(
        """
        CREATE TABLE agent_spans (
            id bigint NOT NULL DEFAULT nextval('agent_spans_id_seq'::regclass),
            created_at timestamp with time zone DEFAULT now(),
            updated_at timestamp with time zone DEFAULT now(),
            trace_id character varying(32) NOT NULL,
            span_id character varying(32) NOT NULL,
            parent_span_id character varying(32),
            name character varying(100) NOT NULL,
            kind character varying(20) NOT NULL,
            agent_name character varying(64),
            agent_version character varying(32),
            tool_name character varying(64),
            model_name character varying(100),
            model_version character varying(100),
            status character varying(20),
            error_type character varying(64),
            latency_ms integer,
            prompt_tokens integer,
            completion_tokens integer,
            total_tokens integer,
            attributes jsonb,
            tenant_id bigint DEFAULT '1'::bigint NOT NULL
        )
        """
    )
    op.execute("ALTER SEQUENCE agent_spans_id_seq OWNED BY agent_spans.id")
    op.execute("ALTER TABLE agent_spans ADD CONSTRAINT agent_spans_pkey PRIMARY KEY (id)")
    op.execute("CREATE INDEX ix_agent_spans_created ON agent_spans USING btree (created_at)")
    op.execute("CREATE INDEX ix_agent_spans_tenant ON agent_spans USING btree (tenant_id)")
    op.execute("CREATE INDEX ix_agent_spans_trace ON agent_spans USING btree (trace_id, id)")
