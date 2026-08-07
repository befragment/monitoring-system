"""Люди, подписки и дежурства — пп. 5.5, 5.6, 7.1 ТЗ.

Сам перечень ролей живёт в `role.py`: на него ссылается ещё и жизненный цикл
проблемы, а импорт пользователя ради одной проверки прав дал бы цикл.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.notification import RoutingChannel
from app.domain.problem import Problem
from app.domain.role import Role
from app.domain.severity import Severity


@dataclass(slots=True)
class User:
    """П. 7.1 делегирует проверку учётных данных LDAP/AD или Keycloak, поэтому
    пароля здесь нет: платформа хранит, кто это и что ему можно видеть, но
    никогда — как он это доказывает.

    `is_active` — точка подключения автоотписки при увольнении из п. 5.6:
    синхронизация с кадровым каталогом переключает флаг, и маршрутизация
    пропускает неактивных, не удаляя историю их подписок.
    """

    id: uuid.UUID
    external_id: str  # идентификатор в каталоге (LDAP DN, Keycloak sub)
    email: str
    full_name: str
    role: Role
    is_active: bool = True

    @property
    def receives_escalation(self) -> bool:
        """Попадает ли человек в персональную рассылку последней ступени.

        Только активные L2: broadcast на уволенного или на manager'а, который в
        разборе не участвует, — это шум ровно в тот момент, когда его меньше
        всего можно себе позволить.
        """
        return self.is_active and self.role is Role.L2


@dataclass(slots=True)
class Subscription:
    """П. 5.6 — самостоятельное управление маршрутизацией.

    Привязана к пользователю, а не выведена из роли: ТЗ прямо говорит, что роль
    даёт умолчания, но не должна жёстко определять подписки, — чтобы инженер мог
    заглушить шумный тег без правки определений ролей администратором.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    channel: RoutingChannel
    # Куда достучаться в этом канале: почтовый ящик, chat id, URL вебхука.
    # Хранится непрозрачно, потому что читать это умеет только адаптер канала.
    address: str
    min_severity: Severity = Severity.WARNING
    # Пусто означает «без фильтра по тегам». Непустое требует совпадения хотя бы
    # по одному тегу — так выражается требование п. 7.1 «группа видит только
    # относящиеся к ней показатели» без отдельной системы ACL. Без этого фильтра
    # любой подписчик получал бы вообще всё, что выше его порога критичности.
    tags: frozenset[str] = field(default_factory=frozenset)
    is_enabled: bool = True

    def matches(self, problem: Problem) -> bool:
        """Хочет ли эта подписка знать о проблеме.

        Чистая и без побочных эффектов, чтобы решения маршрутизации можно было
        покрыть юнит-тестами и, что полезнее, *объяснить*: на вопрос «почему мне
        не пришло» отвечают вызовом этой функции и просмотром того, какое из
        условий вернуло False.
        """
        if not self.is_enabled:
            return False
        if problem.severity < self.min_severity:
            return False
        if self.tags and not (self.tags & problem.tags):
            return False
        return True


@dataclass(frozen=True, slots=True)
class DutyShift:
    """П. 5.5 — таблица дежурств.

    Заготовка на перспективу: текущая лестница эскалации адресуется командным
    каналам и broadcast'у, поэтому выбирать одного человека из группы ей не
    нужно. Понадобится, когда вторая ступень станет персональной.

    Frozen: уже прошедшее дежурство — исторический факт, и журнал аудита обязан
    уметь ответить, кто дежурил в момент эскалации, спустя долгое время после
    того, как график поменялся. Изменение смены означает новую запись.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime

    def covers(self, at: datetime) -> bool:
        # Полуоткрытый интервал: при передаче смены в 09:00 дежурный ровно один,
        # а не двое — и не ноль.
        return self.starts_at <= at < self.ends_at
