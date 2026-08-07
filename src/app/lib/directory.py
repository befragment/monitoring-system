"""Корпоративный каталог: LDAP/AD или Keycloak (п. 7.1 ТЗ).

Единственное место в проекте, через которое проходит пароль, — и он здесь не
задерживается. Платформа не хранит учётные данные и не умеет их проверять сама;
из этого следует, что в таблице `users` нет колонки с хэшем, а `User` в домене
не имеет поля пароля.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DirectoryProfile:
    """То, что каталог знает о человеке.

    Роли здесь нет намеренно: каталог отвечает, кто это, а что ему можно —
    решает платформа. Иначе смена прав в системе оповещений требовала бы правки
    в кадровом каталоге, к которому у команды мониторинга обычно нет доступа.
    """

    external_id: str
    email: str
    full_name: str


class DirectoryGatewayInterface(Protocol):
    async def authenticate(self, login: str, password: str) -> DirectoryProfile | None:
        """None означает «каталог не подтвердил».

        Без различия между «нет такого» и «пароль неверен»: иначе форма входа
        превращается в оракул для перебора логинов.
        """
        ...

    async def list_active_ids(self) -> Sequence[str]:
        """Кто ещё числится в каталоге.

        Отсутствующие гасятся флагом `is_active` (п. 5.6), а их подписки
        остаются в истории — удаление унесло бы вместе с человеком и владение
        проблемами, и ответ на вопрос, кому уходили оповещения.
        """
        ...


class StaticDirectory:
    """Каталог из словаря — для локального прогона и демонстрации.

    Настоящий адаптер (ldap3 или Keycloak по OIDC) подключается вместо этого
    класса без единой правки в сервисах: контракт у них общий. Пароли здесь
    лежат открытым текстом, поэтому в продовой сборке этот класс использоваться
    не должен — он существует, чтобы сквозной путь можно было показать до того,
    как выдан доступ к корпоративному каталогу.
    """

    def __init__(self, people: Mapping[str, tuple[str, DirectoryProfile]]) -> None:
        self._people = dict(people)

    async def authenticate(self, login: str, password: str) -> DirectoryProfile | None:
        record = self._people.get(login)
        if record is None:
            return None
        expected, profile = record
        # Сравнение не постоянного времени — ещё одна причина не выпускать этот
        # класс за пределы демонстрационного стенда.
        return profile if password == expected else None

    async def list_active_ids(self) -> Sequence[str]:
        return [profile.external_id for _, profile in self._people.values()]
