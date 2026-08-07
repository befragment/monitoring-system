"""Порты хранилища: то, что сервисный слой вправе спросить у базы.

`Protocol`, а не абстрактные классы: реализация не наследуется от контракта, а
удовлетворяет ему структурно. Практическая разница — в тестах. Подделка
репозитория пишется как обычный класс со словарём внутри, без импорта чего-либо
из `repository`, и сервис не отличит её от настоящей.

Направление зависимости здесь важнее удобства: интерфейсы объявлены со стороны
того, кто их *использует*, и оперируют доменными объектами. Ни одна сигнатура
ниже не упоминает SQLAlchemy, сессию или строку таблицы — иначе замена хранилища
означала бы переписывание сервисов.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from app.domain.audit import AuditRecord
from app.domain.event import Event, Fingerprint
from app.domain.integration import Integration
from app.domain.notification import Delivery, DeliveryStatus
from app.domain.problem import Problem, ProblemStatus
from app.domain.role import Role
from app.domain.severity import Severity
from app.domain.suppression import MaintenanceWindow
from app.domain.user import Subscription, User


class ProblemAlreadyOpen(Exception):
    """Частичный уникальный индекс отклонил вставку: по этому fingerprint уже
    есть незакрытая проблема.

    Объявлено здесь, а не в сервисе, потому что это часть контракта хранилища:
    гонка двух воркеров, одновременно решивших открыть проблему по одному
    событию, — штатный исход при 50 событиях/сек, и сервис обязан его обработать
    (перечитать проблему и зарегистрировать повтор), а не считать сбоем.
    """


class UnitOfWork(Protocol):
    """Граница транзакции.

    Коммит принадлежит сервису, а не репозиторию: одно действие дежурного —
    это проблема, запись аудита и пачка доставок сразу, и разваливать их на
    отдельные транзакции значит допускать состояние, где действие произошло, а
    журнал о нём молчит.
    """

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UserRepository(Protocol):
    async def get_by_id(self, user_id: uuid.UUID) -> User | None: ...

    async def get_by_external_id(self, external_id: str) -> User | None:
        """Точка входа аутентификации: каталог знает свой идентификатор, не наш."""
        ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def list_by_role(self, role: Role, *, only_active: bool = True) -> list[User]:
        """Адресация ступеней эскалации и рассылок по ролям."""
        ...

    async def list_all(self, *, only_active: bool = False) -> list[User]: ...

    async def add(self, user: User) -> None: ...

    async def save(self, user: User) -> None: ...


class IntegrationRepository(Protocol):
    async def get_by_id(self, integration_id: uuid.UUID) -> Integration | None: ...

    async def get_by_slug(self, slug: str) -> Integration | None:
        """Горячий путь приёма: по строке из `source_system` payload'а."""
        ...

    async def list_all(self, *, only_enabled: bool = False) -> list[Integration]: ...

    async def add(self, integration: Integration) -> None: ...

    async def save(self, integration: Integration) -> None: ...

    async def delete(self, integration_id: uuid.UUID) -> None: ...


class EventRepository(Protocol):
    """Журнал событий. Метода `save` здесь нет намеренно — п. 4.3 ТЗ объявляет
    событие неизменяемой записью, и отсутствие обновления в контракте это
    выражает (в базе то же самое подкреплено триггером)."""

    async def add(self, event: Event, *, problem_id: uuid.UUID | None = None) -> None: ...

    async def exists(self, source_system: str, source_event_id: str) -> bool:
        """Идемпотентность приёма: источник ретраит webhook, и то же событие не
        должно во второй раз увеличить счётчик повторов проблемы."""
        ...

    async def list_for_problem(self, problem_id: uuid.UUID, *, limit: int = 100) -> list[Event]: ...

    async def count_since(self, since: datetime) -> int:
        """«Число обработанных сообщений за сутки» для дашборда п. 7.3."""
        ...


class ProblemRepository(Protocol):
    async def get(self, problem_id: uuid.UUID) -> Problem | None: ...

    async def find_open_by_fingerprint(self, fingerprint: Fingerprint) -> Problem | None:
        """Сердце дедупликации (п. 4.2).

        Ищет только среди незакрытых: закрытая проблема не воскресает, и тот же
        fingerprint через час обязан открыть новый эпизод. Реализация обязана
        опираться на частичный уникальный индекс `WHERE status <> 'closed'`.
        """
        ...

    async def add(self, problem: Problem) -> None:
        """Бросает `ProblemAlreadyOpen`, если гонка опередила."""
        ...

    async def save(self, problem: Problem) -> None: ...

    async def list_escalatable(self, *, limit: int = 100) -> list[Problem]:
        """Проблемы, по которым лестница ещё идёт, — то есть статус ACTIVE.

        Реализация обязана брать строки с блокировкой и пропуском занятых
        (`FOR UPDATE SKIP LOCKED`), иначе два экземпляра планировщика отправят
        одну и ту же ступень дважды.
        """
        ...

    async def search(
        self,
        *,
        statuses: Sequence[ProblemStatus] | None = None,
        min_severity: Severity | None = None,
        asset_name: str | None = None,
        tags: Sequence[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Problem]:
        """Список проблем для дежурного и для интерфейса."""
        ...

    async def count_open(self) -> int: ...


class SubscriptionRepository(Protocol):
    async def get(self, subscription_id: uuid.UUID) -> Subscription | None: ...

    async def list_for_user(self, user_id: uuid.UUID) -> list[Subscription]: ...

    async def list_candidates(
        self, *, severity: Severity, tags: Sequence[str]
    ) -> list[Subscription]:
        """Предварительный отбор подписок под проблему.

        Именно предварительный: окончательное решение принимает
        `Subscription.matches`, потому что правило маршрутизации обязано жить в
        одном месте и быть проверяемым без базы. Репозиторий лишь отсекает
        заведомо лишнее (выключенные, с порогом выше severity) индексами, чтобы
        не тащить в память все подписки системы на каждое событие.
        """
        ...

    async def add(self, subscription: Subscription) -> None: ...

    async def save(self, subscription: Subscription) -> None: ...

    async def delete(self, subscription_id: uuid.UUID) -> None: ...


class DeliveryRepository(Protocol):
    async def get(self, delivery_id: uuid.UUID) -> Delivery | None: ...

    async def add_many(self, deliveries: Sequence[Delivery]) -> None: ...

    async def save(self, delivery: Delivery) -> None: ...

    async def claim_pending(self, *, limit: int = 50) -> list[Delivery]:
        """Очередь отправки. Как и `list_escalatable`, обязана блокировать
        строки с пропуском занятых, иначе два воркера отправят одно дважды."""
        ...

    async def list_for_problem(self, problem_id: uuid.UUID) -> list[Delivery]:
        """Ответ на «почему мне не пришло»."""
        ...

    async def count_for_recipient_since(
        self, recipient_id: uuid.UUID, since: datetime
    ) -> int:
        """Лимит на получателя из п. 4.6."""
        ...

    async def count_by_status_since(self, since: datetime) -> Mapping[DeliveryStatus, int]:
        """Доля доставленных и разбивка по остальным — дашборд п. 7.3."""
        ...


class MaintenanceRepository(Protocol):
    async def find_active_for_asset(
        self, asset_name: str, at: datetime
    ) -> MaintenanceWindow | None:
        """Проверка на горячем пути: под регламентом ли объект прямо сейчас."""
        ...

    async def list_active(self, at: datetime) -> list[MaintenanceWindow]: ...

    async def add(self, window: MaintenanceWindow) -> None: ...

    async def end_now(self, window_id: uuid.UUID, at: datetime) -> None:
        """Снять окно досрочно: работы закончились раньше, оповещения должны
        пойти немедленно."""
        ...


class AuditRepository(Protocol):
    """Только добавление и чтение. Ни `save`, ни `delete` в контракте нет —
    п. 6.3 ТЗ, и в базе это же подкреплено триггером."""

    async def add(self, record: AuditRecord) -> None: ...

    async def list_for_problem(self, problem_id: uuid.UUID) -> list[AuditRecord]: ...

    async def list_recent(self, *, limit: int = 100) -> list[AuditRecord]: ...
