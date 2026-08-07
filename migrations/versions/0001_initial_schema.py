"""Начальная схема: события, проблемы, доставка, подписки, аудит.

Revision ID: 0001
Revises:
Create Date: 2026-08-07

Значения перечислений вписаны литералами, а не импортированы из `app.domain`.
Миграция — это снимок схемы на момент времени: если завтра в `AuditAction`
добавится действие, ограничение в *этой* ревизии обязано остаться прежним, а
новое значение приедет отдельной миграцией. Импорт домена сделал бы историю
схемы задним числом изменяемой.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# VARCHAR + CHECK вместо нативных типов PostgreSQL: нативный enum требует
# ALTER TYPE на каждое новое значение, а эти списки будут расти.
SEVERITY = ("OK", "INFO", "WARNING", "MAJOR", "CRITICAL")
ROLE = ("l1", "l2", "manager", "admin")
CONNECTION_TYPE = ("push", "pull")
ROUTING_CHANNEL = ("webhook", "mail", "mattermost", "telegram", "trueconf")
PROBLEM_STATUS = ("active", "acknowledged", "closed")
PROBLEM_LEVEL = ("l1", "l2")
DELIVERY_STATUS = ("pending", "delivered", "failed", "suppressed")
SUPPRESSION_REASON = ("none", "maintenance", "recipient_rate_limit", "source_rate_limit")
AUDIT_ACTION = (
    "problem_created",
    "problem_acknowledged",
    "problem_handed_over",
    "problem_closed",
    "problem_resolved",
    "notification_sent",
    "notification_suppressed",
    "escalation_triggered",
    "maintenance_started",
    "maintenance_ended",
    "integration_changed",
    "settings_changed",
    "subscription_changed",
)


def _enum(values: Sequence[str], name: str) -> sa.Enum:
    # `create_constraint=True` обязателен: по умолчанию в SQLAlchemy 2.x он
    # выключен, и колонка стала бы обычным VARCHAR, принимающим что угодно.
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    # ---------------------------------------------------------------- users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Идентификатор в каталоге (LDAP DN, Keycloak sub). Пароля здесь нет:
        # п. 7.1 ТЗ делегирует проверку учётных данных каталогу.
        sa.Column("external_id", sa.String(512), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role", _enum(ROLE, "user_role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_external_id", "users", ["external_id"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])
    op.create_index("ix_users_is_active", "users", ["is_active"])

    # --------------------------------------------------------- integrations
    op.create_table(
        "integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("connection", _enum(CONNECTION_TYPE, "connection_type"), nullable=False),
        sa.Column("severity_mapping", postgresql.JSONB(), nullable=False),
        sa.Column("intake_max_events", sa.Integer(), nullable=False),
        sa.Column("intake_per_seconds", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(1024), nullable=True),
        sa.Column("credentials_ref", sa.String(512), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("intake_max_events >= 1", name="ck_integrations_limit_positive"),
        sa.CheckConstraint("intake_per_seconds > 0", name="ck_integrations_window_positive"),
        sa.CheckConstraint("length(btrim(slug)) > 0", name="ck_integrations_slug_not_blank"),
    )
    op.create_index("ix_integrations_slug", "integrations", ["slug"], unique=True)

    # -------------------------------------------------------- subscriptions
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", _enum(ROUTING_CHANNEL, "routing_channel"), nullable=False),
        sa.Column("address", sa.String(1024), nullable=False),
        sa.Column("min_severity", _enum(SEVERITY, "min_severity"), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "channel", "address", name="uq_subscriptions_target"),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_tags", "subscriptions", ["tags"], postgresql_using="gin")

    # --------------------------------------------------------------- events
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("source_event_id", sa.String(255), nullable=False),
        sa.Column("monitor_name", sa.String(512), nullable=False),
        sa.Column("raw_severity", sa.String(64), nullable=False),
        sa.Column("severity", _enum(SEVERITY, "event_severity"), nullable=False),
        sa.Column("asset_name", sa.String(512), nullable=False),
        sa.Column("asset_type", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("problem_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        # Идемпотентность приёма: источник ретраит webhook, и то же событие не
        # должно попасть в журнал дважды.
        sa.UniqueConstraint("source_system", "source_event_id", name="uq_events_source_identity"),
    )
    op.create_index("ix_events_source_system", "events", ["source_system"])
    op.create_index("ix_events_asset_name", "events", ["asset_name"])
    op.create_index("ix_events_occurred_at", "events", ["occurred_at"])
    op.create_index("ix_events_received_at", "events", ["received_at"])
    op.create_index("ix_events_fingerprint", "events", ["fingerprint"])
    op.create_index("ix_events_problem_id", "events", ["problem_id"])
    op.create_index("ix_events_tags", "events", ["tags"], postgresql_using="gin")

    # ------------------------------------------------------------- problems
    op.create_table(
        "problems",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("source_system", sa.String(64), nullable=False),
        sa.Column("monitor_name", sa.String(512), nullable=False),
        sa.Column("asset_name", sa.String(512), nullable=False),
        sa.Column("asset_type", sa.String(64), nullable=False),
        sa.Column("severity", _enum(SEVERITY, "problem_severity"), nullable=False),
        sa.Column("previous_severity", _enum(SEVERITY, "problem_prev_severity"), nullable=False),
        sa.Column("status", _enum(PROBLEM_STATUS, "problem_status"), nullable=False),
        sa.Column("level", _enum(PROBLEM_LEVEL, "problem_level"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("last_message", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_steps_taken", sa.Integer(), server_default="0", nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by", sa.String(255), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("event_count >= 1", name="ck_problems_event_count_positive"),
        sa.CheckConstraint("escalation_steps_taken >= 0", name="ck_problems_escalation_non_negative"),
        # П. 6.4 требует, чтобы закрытие без причины было технически невозможно.
        # В домене это `CloseReasonRequired`, но датакласс не остановит прямой
        # UPDATE — поэтому правило продублировано на уровне базы.
        sa.CheckConstraint(
            "status <> 'closed' OR ("
            "close_reason IS NOT NULL AND btrim(close_reason) <> '' "
            "AND closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_problems_closed_has_reason",
        ),
        sa.CheckConstraint(
            "status <> 'acknowledged' OR (owner_id IS NOT NULL AND acknowledged_at IS NOT NULL)",
            name="ck_problems_acknowledged_has_owner",
        ),
    )
    op.create_index("ix_problems_source_system", "problems", ["source_system"])
    op.create_index("ix_problems_asset_name", "problems", ["asset_name"])
    op.create_index("ix_problems_severity", "problems", ["severity"])
    op.create_index("ix_problems_status", "problems", ["status"])
    op.create_index("ix_problems_level", "problems", ["level"])
    op.create_index("ix_problems_first_seen_at", "problems", ["first_seen_at"])
    op.create_index("ix_problems_last_seen_at", "problems", ["last_seen_at"])
    op.create_index("ix_problems_owner_id", "problems", ["owner_id"])
    op.create_index("ix_problems_fingerprint", "problems", ["fingerprint"])
    op.create_index("ix_problems_tags", "problems", ["tags"], postgresql_using="gin")
    op.create_index("ix_problems_open_by_time", "problems", ["status", "first_seen_at"])
    # Главный индекс системы. Частичный, потому что дедупликация ищет совпадение
    # только среди незакрытых проблем: закрытая не воскресает, и тот же
    # fingerprint через час открывает новый эпизод. Уникальность заодно снимает
    # гонку двух воркеров, одновременно решивших создать проблему по одному
    # fingerprint, — второй получит конфликт и уйдёт в ветку «зарегистрировать
    # повтор».
    op.create_index(
        "uq_problems_open_fingerprint",
        "problems",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )

    # ----------------------------------------------------------- deliveries
    op.create_table(
        "deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("problem_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", _enum(ROUTING_CHANNEL, "delivery_channel"), nullable=False),
        sa.Column("address", sa.String(1024), nullable=False),
        sa.Column("status", _enum(DELIVERY_STATUS, "delivery_status"), nullable=False),
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("escalation_step", sa.Integer(), nullable=True),
        sa.Column("suppression_reason", _enum(SUPPRESSION_REASON, "suppression_reason"), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["problem_id"], ["problems.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.CheckConstraint("attempts >= 0", name="ck_deliveries_attempts_non_negative"),
        # «Не дошло» без объяснения не отвечает ни на один вопрос п. 7.3.
        sa.CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name="ck_deliveries_failure_has_reason",
        ),
        sa.CheckConstraint(
            "status <> 'suppressed' OR suppression_reason IS NOT NULL",
            name="ck_deliveries_suppression_has_reason",
        ),
    )
    op.create_index("ix_deliveries_problem_id", "deliveries", ["problem_id"])
    op.create_index("ix_deliveries_status", "deliveries", ["status"])
    op.create_index("ix_deliveries_recipient_id", "deliveries", ["recipient_id"])
    op.create_index("ix_deliveries_created_at", "deliveries", ["created_at"])
    op.create_index("ix_deliveries_pending", "deliveries", ["status", "created_at"])

    # --------------------------------------------------- maintenance_windows
    op.create_table(
        "maintenance_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_name", sa.String(512), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        # Пустое имя объекта заглушило бы всю платформу — ровно тот способ
        # спрятать аварию, ради предотвращения которого ограничение и заведено.
        sa.CheckConstraint("length(btrim(asset_name)) > 0", name="ck_maintenance_asset_not_blank"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_maintenance_reason_not_blank"),
        # Окно обязано само выключаться.
        sa.CheckConstraint("ends_at > starts_at", name="ck_maintenance_ends_after_start"),
    )
    op.create_index("ix_maintenance_windows_asset_name", "maintenance_windows", ["asset_name"])
    op.create_index(
        "ix_maintenance_lookup", "maintenance_windows", ["asset_name", "starts_at", "ends_at"]
    )

    # ---------------------------------------------------------- duty_shifts
    op.create_table(
        "duty_shifts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_duty_ends_after_start"),
    )
    op.create_index("ix_duty_shifts_user_id", "duty_shifts", ["user_id"])
    op.create_index("ix_duty_shifts_period", "duty_shifts", ["starts_at", "ends_at"])

    # -------------------------------------------------------- audit_records
    op.create_table(
        "audit_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", _enum(AUDIT_ACTION, "audit_action"), nullable=False),
        # Внешних ключей нет намеренно: журнал обязан пережить своих субъектов.
        # Удаление старых проблем по TTL (п. 7.2) не должно ни каскадом стирать
        # историю, ни блокироваться ссылкой из неё.
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("problem_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_records_occurred_at", "audit_records", ["occurred_at"])
    op.create_index("ix_audit_records_action", "audit_records", ["action"])
    op.create_index("ix_audit_records_actor_id", "audit_records", ["actor_id"])
    op.create_index("ix_audit_records_problem_id", "audit_records", ["problem_id"])

    # ------------------------------------------------ неизменяемость записей
    # П. 6.3 требует, чтобы журнал допускал только добавление, а п. 4.3 — чтобы
    # событие было неизменяемой записью. `frozen=True` в домене выражает
    # намерение, но не остановит `session.execute(update(...))`. Настоящий
    # запрет ставится здесь.
    op.execute(
        """
        CREATE FUNCTION reject_write() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is not allowed on table %', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    # Аудит: ни правок, ни удалений. Чистка по сроку хранения потребует
    # осознанного отключения триггера отдельной миграцией — и это правильно.
    op.execute(
        "CREATE TRIGGER audit_records_append_only "
        "BEFORE UPDATE OR DELETE ON audit_records "
        "FOR EACH ROW EXECUTE FUNCTION reject_write();"
    )
    # События: правки запрещены, а удаление разрешено — по нему работает TTL из
    # п. 7.2, где горячие данные держатся дни-недели, а дальше уходят в агрегаты.
    op.execute(
        "CREATE TRIGGER events_immutable "
        "BEFORE UPDATE ON events "
        "FOR EACH ROW EXECUTE FUNCTION reject_write();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS events_immutable ON events;")
    op.execute("DROP TRIGGER IF EXISTS audit_records_append_only ON audit_records;")
    op.execute("DROP FUNCTION IF EXISTS reject_write();")

    op.drop_table("audit_records")
    op.drop_table("duty_shifts")
    op.drop_table("maintenance_windows")
    op.drop_table("deliveries")
    op.drop_table("problems")
    op.drop_table("events")
    op.drop_table("subscriptions")
    op.drop_table("integrations")
    op.drop_table("users")
