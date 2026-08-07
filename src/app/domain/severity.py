"""Шаг 3 — классификация: severity источников приводится к единой шкале."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    """Единая шкала платформы из п. 3.1 ТЗ.

    IntEnum, а не StrEnum, потому что критичность действительно упорядочена:
    п. 4.3 говорит, что проблема сохраняет идентичность, пока её критичность
    меняется, — значит код обязан уметь спросить «стало хуже?» (`new > old`), а
    подписки фильтруют по «не ниже такого-то уровня» (п. 5.6). Имя пишется в
    БД, порядок живёт в типе — читаемо и сравнимо одновременно.
    """

    OK = 0
    INFO = 1
    WARNING = 2
    MAJOR = 3
    CRITICAL = 4


@dataclass(frozen=True, slots=True)
class SeverityMapping:
    """Отдельная таблица соответствий на каждый источник (п. 3.1 ТЗ).

    Именно на источник, а не одна общая: «average» означает вполне конкретную
    вещь в Zabbix и ничего в Prometheus, поэтому общая таблица быстро набралась
    бы неоднозначных ключей.
    """

    source_system: str
    table: Mapping[str, Severity]
    # Незнакомая severity — это дыра в конфигурации, и ни одна крайность не
    # безопасна как значение по умолчанию: INFO похоронит реальную аварию, а
    # CRITICAL поднимет человека в три часа ночи из-за опечатки в настройках
    # источника. WARNING заметен, но никого не будит, — а это ровно то
    # поведение, которое нужно, пока дыру не обнаружили.
    fallback: Severity = Severity.WARNING

    def classify(self, raw_severity: str) -> Severity:
        """Чистый поиск по таблице.

        П. 3.2 ТЗ запрещает анализировать здесь текст сообщения: классификация
        обязана оставаться детерминированной и объяснимой, а формулировки
        сообщений плавают от версии к версии источника и незаметно меняли бы
        класс события.
        """
        return self.table.get(raw_severity.strip().lower(), self.fallback)


# У Zabbix шесть именованных severity; это первая интеграция по п. 1.1 ТЗ.
# «not classified» намеренно отображён ниже WARNING: Zabbix ставит его триггерам,
# которые ещё никто не разобрал, — это информация, а не инцидент.
ZABBIX_SEVERITY_MAPPING = SeverityMapping(
    source_system="zabbix",
    table={
        "not classified": Severity.INFO,
        "information": Severity.INFO,
        "warning": Severity.WARNING,
        "average": Severity.MAJOR,
        "high": Severity.CRITICAL,
        "disaster": Severity.CRITICAL,
        # Webhook'и Zabbix шлют ещё и состояние «resolved»/«OK» для событий
        # восстановления — именно оно автоматически закрывает проблему
        # (см. Problem.resolve).
        "ok": Severity.OK,
        "resolved": Severity.OK,
    },
)
