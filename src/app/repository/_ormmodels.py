"""Отображение домена на таблицы PostgreSQL.

Все модели лежат в одном модуле намеренно: `migrations/env.py` импортирует его
целиком, чтобы таблицы зарегистрировались в `Base.metadata`, а внешние ключи
между агрегатами иначе потребовали бы строковых ссылок и аккуратного порядка
импортов.

Правило слоя: ORM-модели не содержат бизнес-логики. Они описывают форму
хранения и те инварианты, которые обязана держать *база*, — то есть ровно те,
на которые датакласс не способен: уникальность, ссылочная целостность и запрет
на состояния, недостижимые через домен, но достижимые через прямой UPDATE.
Всё остальное живёт в `app.domain` и проверяется тестами там.
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.audit import AuditAction
from app.domain.integration import ConnectionType
from app.domain.notification import DeliveryStatus, RoutingChannel
from app.domain.problem import ProblemLevel, ProblemStatus
from app.domain.role import Role
from app.domain.severity import Severity
from app.domain.suppression import SuppressionReason
from app.lib.postgres import Base


def _enum(enum_cls: type[PyEnum], name: str) -> sa.Enum:
    """VARCHAR + CHECK вместо нативного типа PostgreSQL.

    Нативный enum требует `ALTER TYPE` на каждое новое значение, а эти списки
    будут расти — словарь действий аудита и набор каналов доставки в первую
    очередь. VARCHAR с ограничением даёт ту же проверку и переживает миграцию
    обычным ALTER.

    `values_callable` заставляет писать в базу *значение* StrEnum («l1»), а не
    имя («L1»). Значение — это то, что уходит в claim токена и в JSON API, и
    расхождение между строкой в токене и строкой в колонке обнаружилось бы уже
    в проде.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        # В SQLAlchemy 2.x по умолчанию выключено, и без этого получился бы
        # просто VARCHAR: база приняла бы любую строку, а «проверка» жила бы
        # только в питоне.
        create_constraint=True,
        values_callable=lambda enum: [member.value for member in enum],
        validate_strings=True,
    )


def _severity(name: str = "severity") -> sa.Enum:
    """Критичность хранится *именем*, а не числом.

    Домен объявляет, что порядок живёт в типе (`IntEnum`), а в базе значение
    должно читаться глазами: «CRITICAL» в выборке понятнее, чем «4». Здесь
    `values_callable` намеренно не задан — SQLAlchemy по умолчанию пишет имя
    члена, что для IntEnum как раз и нужно.

    Цена решения: сравнение диапазоном в SQL напрямую не работает, фильтр «не
    ниже WARNING» выражается через `IN (...)`. Это устраивает, потому что
    уровней всего пять и список конечен.
    """
    return sa.Enum(
        Severity,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _tags() -> Mapped[list[str]]:
    """Теги массивом, а не JSON: по массиву работает GIN-индекс и оператор
    пересечения `&&`, а маршрутизация спрашивает именно «есть ли общий тег»."""
    return mapped_column(ARRAY(sa.Text), nullable=False, server_default="{}")


# --------------------------------------------------------------------------
# Конфигурация: то, что заводит администратор
# --------------------------------------------------------------------------


class IntegrationORM(Base):
    """Подключённая система мониторинга (Шаг 1)."""

    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Естественный ключ: именно эта строка приходит в `source_system` payload'а
    # и участвует в fingerprint. Уникальность обязательна — две интеграции с
    # одним slug схлопнули бы свои проблемы в одну.
    slug: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(sa.String(255))
    connection: Mapped[ConnectionType] = mapped_column(_enum(ConnectionType, "connection_type"))
    # Своя таблица соответствий на каждую интеграцию (п. 3.1):
    # {"table": {"high": "CRITICAL", ...}, "fallback": "WARNING"}.
    # JSONB, а не отдельная таблица строк: читается всегда целиком, вместе с
    # интеграцией, и никогда не опрашивается по одному ключу.
    severity_mapping: Mapped[dict] = mapped_column(JSONB)
    # П. 2.5 — лимит приёма. Двумя колонками, а не JSON: по ним строят отчёты.
    intake_max_events: Mapped[int] = mapped_column(sa.Integer, default=100)
    intake_per_seconds: Mapped[int] = mapped_column(sa.Integer, default=60)
    # Для pull — что опрашивать; для push инициатива на той стороне.
    endpoint: Mapped[str | None] = mapped_column(sa.String(1024), nullable=True)
    # Ссылка на секрет в хранилище, а не сам секрет: в базе приложения не должно
    # лежать ничего, чем можно воспользоваться напрямую.
    credentials_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint("intake_max_events >= 1", name="ck_integrations_limit_positive"),
        sa.CheckConstraint("intake_per_seconds > 0", name="ck_integrations_window_positive"),
        sa.CheckConstraint("length(btrim(slug)) > 0", name="ck_integrations_slug_not_blank"),
    )


class UserORM(Base):
    """Человек. Пароля здесь нет и быть не должно.

    П. 7.1 ТЗ делегирует проверку учётных данных LDAP/AD или Keycloak: платформа
    хранит, кто это и что ему можно видеть, но никогда — как он это доказывает.
    Колонка с хэшем пароля означала бы вторую систему аутентификации, которую
    никто не собирался синхронизировать с каталогом.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Идентификатор в каталоге: LDAP DN или Keycloak sub. По нему сходится
    # синхронизация, поэтому он уникален и индексирован.
    external_id: Mapped[str] = mapped_column(sa.String(512), unique=True, index=True)
    email: Mapped[str] = mapped_column(sa.String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(sa.String(255))
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"), index=True)
    # Точка подключения автоотписки при увольнении (п. 5.6): синхронизация с
    # кадровым каталогом гасит флаг, маршрутизация пропускает неактивных, а
    # история подписок остаётся нетронутой.
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, default=True, server_default=sa.true(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


class SubscriptionORM(Base):
    """П. 5.6 — правило маршрутизации, которым владеет сам пользователь."""

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[RoutingChannel] = mapped_column(_enum(RoutingChannel, "routing_channel"))
    # Почтовый ящик, chat id, URL вебхука. Непрозрачно для платформы: читать
    # это умеет только адаптер канала.
    address: Mapped[str] = mapped_column(sa.String(1024))
    min_severity: Mapped[Severity] = mapped_column(_severity("min_severity"))
    # Пусто — фильтра нет. Непустое требует совпадения хотя бы по одному тегу:
    # так выражается «группа видит только относящиеся к ней показатели» (п. 7.1).
    tags: Mapped[list[str]] = _tags()
    is_enabled: Mapped[bool] = mapped_column(sa.Boolean, default=True, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )

    __table_args__ = (
        # Один и тот же адрес в одном канале дважды — это просто дубль
        # уведомлений, а не осмысленная настройка.
        sa.UniqueConstraint("user_id", "channel", "address", name="uq_subscriptions_target"),
        sa.Index("ix_subscriptions_tags", "tags", postgresql_using="gin"),
    )


# --------------------------------------------------------------------------
# Поток событий
# --------------------------------------------------------------------------


class EventORM(Base):
    """П. 4.3 — неизменяемая запись журнала.

    Слой хранения обязан подкрепить `frozen=True` домена тем, что по этой
    таблице никогда не выполняется UPDATE. Ценность журнала в том, что можно
    воспроизвести, что именно платформе сообщили: исправленное событие — это
    новая строка, а не правка старой.
    """

    __tablename__ = "events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_system: Mapped[str] = mapped_column(sa.String(64), index=True)
    source_event_id: Mapped[str] = mapped_column(sa.String(255))
    monitor_name: Mapped[str] = mapped_column(sa.String(512))
    raw_severity: Mapped[str] = mapped_column(sa.String(64))
    severity: Mapped[Severity] = mapped_column(_severity("event_severity"))
    asset_name: Mapped[str] = mapped_column(sa.String(512), index=True)
    asset_type: Mapped[str] = mapped_column(sa.String(64))
    message: Mapped[str] = mapped_column(sa.Text)
    # Когда, по словам источника, это произошло, и когда нам сообщили.
    # Расходятся при аварии самой интеграции; TTL по п. 7.2 считается от
    # received_at, а оператор смотрит на occurred_at.
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    tags: Mapped[list[str]] = _tags()
    fingerprint: Mapped[str] = mapped_column(sa.String(64), index=True)
    # Денормализация уровня хранения, которой нет в доменном `Event`: связать
    # событие с эпизодом. Без неё «показать события этой проблемы» пришлось бы
    # выражать через fingerprint плюс временное окно, а после закрытия и
    # повторной поломки тот же fingerprint принадлежит уже другому эпизоду —
    # и выборка смешала бы их. Проставляет сервисный слой.
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )

    __table_args__ = (
        # Идемпотентность приёма: источник ретраит webhook, и то же событие от
        # той же интеграции не должно попасть в журнал дважды.
        sa.UniqueConstraint(
            "source_system", "source_event_id", name="uq_events_source_identity"
        ),
        sa.Index("ix_events_tags", "tags", postgresql_using="gin"),
    )


class ProblemORM(Base):
    """Единственная изменяемая сущность модели."""

    __tablename__ = "problems"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fingerprint: Mapped[str] = mapped_column(sa.String(64))
    # Slug интеграции, а не ссылка на неё: fingerprint строится по строке, и
    # приём события не должен ходить в справочник на горячем пути.
    source_system: Mapped[str] = mapped_column(sa.String(64), index=True)
    monitor_name: Mapped[str] = mapped_column(sa.String(512))
    asset_name: Mapped[str] = mapped_column(sa.String(512), index=True)
    asset_type: Mapped[str] = mapped_column(sa.String(64))

    severity: Mapped[Severity] = mapped_column(_severity("problem_severity"), index=True)
    # Последняя *отличавшаяся* критичность — для `prev_state` в исходящем
    # сообщении (п. 5.1).
    previous_severity: Mapped[Severity] = mapped_column(_severity("problem_prev_severity"))

    status: Mapped[ProblemStatus] = mapped_column(_enum(ProblemStatus, "problem_status"), index=True)
    level: Mapped[ProblemLevel] = mapped_column(_enum(ProblemLevel, "problem_level"), index=True)

    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    # П. 4.2 — счётчик повторов вместо дублирующих проблем.
    event_count: Mapped[int] = mapped_column(sa.Integer, default=1)
    last_message: Mapped[str] = mapped_column(sa.Text)
    tags: Mapped[list[str]] = _tags()

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    handed_over_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Состояние лестницы живёт здесь, а не в политике: без записанного счётчика
    # воркер после перезапуска разослал бы всю лестницу заново.
    escalation_steps_taken: Mapped[int] = mapped_column(sa.Integer, default=0, server_default="0")

    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        # Главный индекс системы. Частичный — потому что дедупликация ищет
        # совпадение только среди незакрытых проблем: закрытая не воскресает, и
        # тот же fingerprint через час открывает новый эпизод. Уникальность
        # заодно снимает гонку двух воркеров, одновременно решивших создать
        # проблему по одному fingerprint, — второй получит конфликт и уйдёт в
        # ветку «зарегистрировать повтор».
        sa.Index(
            "uq_problems_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=sa.text("status <> 'closed'"),
        ),
        sa.Index("ix_problems_fingerprint", "fingerprint"),
        sa.Index("ix_problems_tags", "tags", postgresql_using="gin"),
        # Рабочая выборка воркера эскалации и списка дежурного.
        sa.Index("ix_problems_open_by_time", "status", "first_seen_at"),
        sa.CheckConstraint("event_count >= 1", name="ck_problems_event_count_positive"),
        sa.CheckConstraint(
            "escalation_steps_taken >= 0", name="ck_problems_escalation_non_negative"
        ),
        # П. 6.4 требует, чтобы закрытие без причины было *технически
        # невозможно*. В домене это `CloseReasonRequired`, но датакласс не
        # остановит прямой UPDATE — поэтому то же правило продублировано здесь.
        sa.CheckConstraint(
            "status <> 'closed' OR ("
            "close_reason IS NOT NULL AND btrim(close_reason) <> '' "
            "AND closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_problems_closed_has_reason",
        ),
        # Владелец появляется вместе со статусом «в работе», не раньше.
        sa.CheckConstraint(
            "status <> 'acknowledged' OR (owner_id IS NOT NULL AND acknowledged_at IS NOT NULL)",
            name="ck_problems_acknowledged_has_owner",
        ),
    )


class DeliveryORM(Base):
    """Факт отправки — то, чем отвечают на «почему мне не пришло»."""

    __tablename__ = "deliveries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("problems.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[RoutingChannel] = mapped_column(_enum(RoutingChannel, "delivery_channel"))
    # Копия адреса на момент отправки, а не ссылка на подписку: если человек
    # завтра сменит адрес, история обязана помнить, куда сообщение ушло на
    # самом деле.
    address: Mapped[str] = mapped_column(sa.String(1024))
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus, "delivery_status"), index=True
    )
    # Пусто, когда адресат — командный канал, а не человек.
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Пусто, когда отправку инициировала эскалация, а не подписка. Различие
    # нужно, чтобы «не совпало правило подписки» и «лестница до тебя не дошла»
    # были разными ответами.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    escalation_step: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # Подавление отделено от провала: подавленное по регламенту сообщение не
    # должно портить долю успешной доставки на дашборде (п. 7.3).
    suppression_reason: Mapped[SuppressionReason | None] = mapped_column(
        _enum(SuppressionReason, "suppression_reason"), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        sa.CheckConstraint("attempts >= 0", name="ck_deliveries_attempts_non_negative"),
        sa.CheckConstraint(
            "status <> 'failed' OR failure_reason IS NOT NULL",
            name="ck_deliveries_failure_has_reason",
        ),
        sa.CheckConstraint(
            "status <> 'suppressed' OR suppression_reason IS NOT NULL",
            name="ck_deliveries_suppression_has_reason",
        ),
        # Выборка воркера доставки: забрать всё, что ещё не отправлено.
        sa.Index("ix_deliveries_pending", "status", "created_at"),
    )


# --------------------------------------------------------------------------
# Подавление и дежурства
# --------------------------------------------------------------------------


class MaintenanceWindowORM(Base):
    """П. 4.5 — плановые работы по конкретному объекту."""

    __tablename__ = "maintenance_windows"

    id: Mapped[uuid.UUID] = _uuid_pk()
    asset_name: Mapped[str] = mapped_column(sa.String(512), index=True)
    starts_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    # Кто объявил и зачем — ответ на вопрос, который задают уже после инцидента:
    # «почему в три ночи не было ни одного алерта по этому объекту?».
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT")
    )
    reason: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )

    __table_args__ = (
        # Пустое имя объекта заглушило бы всю платформу — ровно тот способ
        # спрятать аварию, ради предотвращения которого ограничение и заведено.
        sa.CheckConstraint("length(btrim(asset_name)) > 0", name="ck_maintenance_asset_not_blank"),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_maintenance_reason_not_blank"),
        # Окно обязано само выключаться: бессрочное вернуло бы в обиход
        # «заглушил и забыл включить обратно».
        sa.CheckConstraint("ends_at > starts_at", name="ck_maintenance_ends_after_start"),
        # Проверка «объект сейчас под регламентом» на горячем пути.
        sa.Index("ix_maintenance_lookup", "asset_name", "starts_at", "ends_at"),
    )


class DutyShiftORM(Base):
    """П. 5.5 — таблица дежурств. Заготовка: текущая лестница адресуется
    командным каналам, и выбирать одного человека из группы ей пока не нужно."""

    __tablename__ = "duty_shifts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    starts_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("ends_at > starts_at", name="ck_duty_ends_after_start"),
        sa.Index("ix_duty_shifts_period", "starts_at", "ends_at"),
    )


# --------------------------------------------------------------------------
# Аудит
# --------------------------------------------------------------------------


class AuditRecordORM(Base):
    """П. 6.3 — журнал, допускающий только добавление.

    Внешних ключей здесь намеренно нет ни на проблему, ни на пользователя.
    Журнал обязан пережить своих субъектов: удаление старых проблем по TTL
    (п. 7.2) не должно ни каскадом стирать историю, ни блокироваться ссылкой из
    неё. Целостность здесь приносится в жертву долговечности сознательно.

    Настоящий запрет на правку ставит миграция — триггером, отклоняющим UPDATE
    и DELETE. Датакласс с `frozen=True` не остановит `session.execute(update(...))`.
    """

    __tablename__ = "audit_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    occurred_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), index=True)
    action: Mapped[AuditAction] = mapped_column(_enum(AuditAction, "audit_action"), index=True)
    # Пусто, когда действовала сама платформа: автозакрытие по возврату объекта
    # в норму и таймеры эскалации не имеют человека за спиной, а фиктивный id
    # испортил бы выборки «кто это сделал».
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    problem_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # Плоские строки: журнал обязан оставаться читаемым и через годы, не завися
    # от того, существуют ли ещё сегодняшние формы объектов, чтобы его разобрать.
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
