"""Подделки портов и собранные на них сервисы.

Каждая подделка ниже — обычный класс со словарём внутри. Ни одна не наследуется
от контракта и не импортирует ничего из `app.repository` кроме доменных типов:
`Protocol` удовлетворяется структурно, и именно это делает сервисный слой
проверяемым без базы, Redis и телеграма. Если такая подделка перестанет
подходить сервису — значит контракт изменился, и это должно быть видно.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from app.domain.asset import Asset
from app.domain.escalation import DEFAULT_ESCALATION_POLICY
from app.domain.event import IncomingEvent
from app.domain.integration import ConnectionType, Integration
from app.domain.notification import DeliveryStatus, RoutingChannel
from app.domain.problem import ProblemStatus
from app.domain.role import Role
from app.domain.severity import ZABBIX_SEVERITY_MAPPING, Severity
from app.domain.user import Subscription, User
from app.lib.clock import FixedClock
from app.lib.directory import DirectoryProfile
from app.lib.notification import ChannelDeliveryError
from app.repository._contracts import ProblemAlreadyOpen

T0 = datetime(2026, 8, 7, 2, 14, tzinfo=UTC)


# --------------------------------------------------------------------------
# Подделки инфраструктурных портов
# --------------------------------------------------------------------------


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeQueue:
    def __init__(self) -> None:
        self.items: list = []

    async def publish(self, payload):
        self.items.append(payload)

    async def consume(self, *, limit=100):
        out, self.items = self.items[:limit], self.items[limit:]
        return out

    async def size(self):
        return len(self.items)


class FakeRateLimiter:
    """Считает попадания по ключу. Окно игнорируется: тестам важно, сколько раз
    сервис отметил событие и по какому ключу, а не арифметика скользящего окна —
    она проверяется отдельно, на настоящей реализации."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)

    async def hit(self, key, window):
        self.counts[key] += 1
        return self.counts[key]

    async def count(self, key, window):
        return self.counts[key]

    def preload(self, key: str, times: int) -> None:
        self.counts[key] = times


class FakeGateway:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.fail_on: set[RoutingChannel] = set()

    async def send(self, channel, address, message):
        if channel in self.fail_on:
            raise ChannelDeliveryError(f"{channel.value}: 503 service unavailable")
        self.sent.append((channel, address, message))

    async def send_digest(self, channel, address, digest):
        self.sent.append((channel, address, digest))


class FakeChannels:
    def __init__(self, configured=(Role.L1, Role.L2)) -> None:
        self._configured = set(configured)

    def channel_for(self, role):
        if role not in self._configured:
            return None
        return (RoutingChannel.MATTERMOST, f"#{role.value}-duty")


class FakeDirectory:
    def __init__(self, people: dict[str, tuple[str, DirectoryProfile]]) -> None:
        self.people = people

    async def authenticate(self, login, password):
        record = self.people.get(login)
        if record is None:
            return None
        expected, profile = record
        return profile if password == expected else None

    async def list_active_ids(self):
        return [profile.external_id for _, profile in self.people.values()]


class FakeTokens:
    """Возвращает читаемую строку вместо JWT: тест проверяет, что в токен ушли
    те id и роль, а не то, что PyJWT умеет подписывать."""

    def issue_access(self, subject, role):
        return f"access:{subject}:{role}"

    def issue_refresh(self, subject, role):
        return f"refresh:{subject}:{role}"


# --------------------------------------------------------------------------
# Подделки репозиториев
# --------------------------------------------------------------------------


class FakeIntegrations:
    def __init__(self, *items: Integration) -> None:
        self.rows = {i.slug: i for i in items}

    async def get_by_id(self, integration_id):
        return next((i for i in self.rows.values() if i.id == integration_id), None)

    async def get_by_slug(self, slug):
        return self.rows.get(slug)

    async def list_all(self, *, only_enabled=False):
        return [i for i in self.rows.values() if i.is_enabled or not only_enabled]

    async def add(self, integration):
        self.rows[integration.slug] = integration

    async def save(self, integration):
        self.rows[integration.slug] = integration

    async def delete(self, integration_id):
        target = await self.get_by_id(integration_id)
        if target:
            del self.rows[target.slug]


class FakeEvents:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    async def add(self, event, *, problem_id=None):
        self.rows.append((event, problem_id))

    async def exists(self, source_system, source_event_id):
        return any(
            e.source_system == source_system and e.source_event_id == source_event_id
            for e, _ in self.rows
        )

    async def list_for_problem(self, problem_id, *, limit=100):
        return [e for e, p in self.rows if p == problem_id][:limit]

    async def count_since(self, since):
        return sum(1 for e, _ in self.rows if e.received_at >= since)


class FakeProblems:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, object] = {}

    async def get(self, problem_id):
        return self.rows.get(problem_id)

    async def find_open_by_fingerprint(self, fingerprint):
        return next(
            (p for p in self.rows.values() if p.fingerprint == fingerprint and p.is_open),
            None,
        )

    async def add(self, problem):
        # Подделка частичного уникального индекса `WHERE status <> 'closed'`.
        if await self.find_open_by_fingerprint(problem.fingerprint):
            raise ProblemAlreadyOpen(problem.fingerprint.value)
        self.rows[problem.id] = problem

    async def save(self, problem):
        self.rows[problem.id] = problem

    async def list_escalatable(self, *, limit=100):
        return [p for p in self.rows.values() if p.status is ProblemStatus.ACTIVE][:limit]

    async def search(self, **kwargs):
        return list(self.rows.values())

    async def count_open(self):
        return sum(1 for p in self.rows.values() if p.is_open)


class FakeSubscriptions:
    def __init__(self, *items: Subscription) -> None:
        self.rows = {s.id: s for s in items}

    async def get(self, subscription_id):
        return self.rows.get(subscription_id)

    async def list_for_user(self, user_id):
        return [s for s in self.rows.values() if s.user_id == user_id]

    async def list_candidates(self, *, severity, tags):
        # Ровно то, что обещает контракт: предварительный отбор индексами.
        # Окончательное решение остаётся за `Subscription.matches`.
        return [
            s for s in self.rows.values() if s.is_enabled and severity >= s.min_severity
        ]

    async def add(self, subscription):
        self.rows[subscription.id] = subscription

    async def save(self, subscription):
        self.rows[subscription.id] = subscription

    async def delete(self, subscription_id):
        self.rows.pop(subscription_id, None)


class FakeDeliveries:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, object] = {}

    async def get(self, delivery_id):
        return self.rows.get(delivery_id)

    async def add_many(self, deliveries):
        for delivery in deliveries:
            self.rows[delivery.id] = delivery

    async def save(self, delivery):
        self.rows[delivery.id] = delivery

    async def claim_pending(self, *, limit=50):
        return [d for d in self.rows.values() if d.status is DeliveryStatus.PENDING][:limit]

    async def list_for_problem(self, problem_id):
        return [d for d in self.rows.values() if d.problem_id == problem_id]

    async def count_for_recipient_since(self, recipient_id, since):
        return sum(1 for d in self.rows.values() if d.recipient_id == recipient_id)

    async def count_by_status_since(self, since):
        counts: dict = defaultdict(int)
        for delivery in self.rows.values():
            counts[delivery.status] += 1
        return counts

    def by_step(self, step: int) -> list:
        return [d for d in self.rows.values() if d.escalation_step == step]


class FakeMaintenance:
    def __init__(self) -> None:
        self.rows: list = []

    async def find_active_for_asset(self, asset_name, at):
        return next((w for w in self.rows if w.covers(asset_name, at)), None)

    async def list_active(self, at):
        return [w for w in self.rows if w.starts_at <= at < w.ends_at]

    async def add(self, window):
        self.rows.append(window)

    async def end_now(self, window_id, at):
        self.rows = [w for w in self.rows if w.id != window_id]


class FakeAudit:
    def __init__(self) -> None:
        self.rows: list = []

    async def add(self, record):
        self.rows.append(record)

    async def list_for_problem(self, problem_id):
        return [r for r in self.rows if r.problem_id == problem_id]

    async def list_recent(self, *, limit=100):
        return self.rows[-limit:]

    def actions(self) -> list[str]:
        return [r.action.value for r in self.rows]


class FakeUsers:
    def __init__(self, *items: User) -> None:
        self.rows = {u.id: u for u in items}

    async def get_by_id(self, user_id):
        return self.rows.get(user_id)

    async def get_by_external_id(self, external_id):
        return next((u for u in self.rows.values() if u.external_id == external_id), None)

    async def get_by_email(self, email):
        return next((u for u in self.rows.values() if u.email == email), None)

    async def list_by_role(self, role, *, only_active=True):
        return [
            u
            for u in self.rows.values()
            if u.role is role and (u.is_active or not only_active)
        ]

    async def list_all(self, *, only_active=False):
        return [u for u in self.rows.values() if u.is_active or not only_active]

    async def add(self, user):
        self.rows[user.id] = user

    async def save(self, user):
        self.rows[user.id] = user


# --------------------------------------------------------------------------
# Фикстуры
# --------------------------------------------------------------------------


@pytest.fixture
def t0() -> datetime:
    return T0


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def zabbix() -> Integration:
    return Integration(
        id=uuid.uuid4(),
        slug="zabbix",
        title="Zabbix прод",
        connection=ConnectionType.PUSH,
        severity_mapping=ZABBIX_SEVERITY_MAPPING,
    )


def make_user(role: Role, *, is_active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        external_id=f"cn={role.value}",
        email=f"{role.value}@example.ru",
        full_name=f"Пользователь {role.value}",
        role=role,
        is_active=is_active,
    )


@pytest.fixture
def l1() -> User:
    return make_user(Role.L1)


@pytest.fixture
def l2() -> User:
    return make_user(Role.L2)


@pytest.fixture
def manager() -> User:
    return make_user(Role.MANAGER)


@pytest.fixture
def admin() -> User:
    return make_user(Role.ADMIN)


@pytest.fixture
def users(l1, l2, manager, admin) -> FakeUsers:
    return FakeUsers(l1, l2, manager, admin)


@pytest.fixture
def sub_l1(l1) -> Subscription:
    return Subscription(
        id=uuid.uuid4(),
        user_id=l1.id,
        channel=RoutingChannel.TELEGRAM,
        address="tg:111",
        min_severity=Severity.WARNING,
    )


@pytest.fixture
def sub_l2(l2) -> Subscription:
    return Subscription(
        id=uuid.uuid4(),
        user_id=l2.id,
        channel=RoutingChannel.MAIL,
        address="l2@example.ru",
        min_severity=Severity.MAJOR,
    )


@pytest.fixture
def subscriptions(sub_l1, sub_l2) -> FakeSubscriptions:
    return FakeSubscriptions(sub_l1, sub_l2)


@pytest.fixture
def integrations(zabbix) -> FakeIntegrations:
    return FakeIntegrations(zabbix)


@pytest.fixture
def events() -> FakeEvents:
    return FakeEvents()


@pytest.fixture
def problems() -> FakeProblems:
    return FakeProblems()


@pytest.fixture
def deliveries() -> FakeDeliveries:
    return FakeDeliveries()


@pytest.fixture
def maintenance() -> FakeMaintenance:
    return FakeMaintenance()


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def limiter() -> FakeRateLimiter:
    return FakeRateLimiter()


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def channels() -> FakeChannels:
    return FakeChannels()


@pytest.fixture
def routing(subscriptions, deliveries, maintenance, limiter, clock):
    from app.service.routing import RoutingService

    return RoutingService(subscriptions, deliveries, maintenance, limiter, clock)


@pytest.fixture
def processing(integrations, events, problems, audit, routing, uow):
    from app.service.event_processing import EventProcessingService

    return EventProcessingService(integrations, events, problems, audit, routing, uow)


@pytest.fixture
def incoming() -> IncomingEvent:
    """Ровно payload из п. 2.1 ТЗ."""
    return IncomingEvent(
        source_system="zabbix",
        source_event_id="8841203",
        monitor_name="Свободное место на диске",
        raw_severity="high",
        asset=Asset("db-prod-03", "server"),
        message="Диск заполнен на 94%",
        occurred_at=T0,
        received_at=T0,
        tags=frozenset({"disk", "prod", "msk-dc1"}),
    )


@pytest.fixture
def window_now(l1):
    """Окно обслуживания, накрывающее `db-prod-03` прямо сейчас."""
    from app.domain.suppression import MaintenanceWindow

    return MaintenanceWindow(
        id=uuid.uuid4(),
        asset_name="db-prod-03",
        starts_at=T0 - timedelta(hours=1),
        ends_at=T0 + timedelta(hours=2),
        created_by=l1.id,
        reason="Замена диска",
    )
