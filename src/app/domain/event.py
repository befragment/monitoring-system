"""Шаги 2–4 — неизменяемая часть модели: что пришло и какова его идентичность."""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.domain.asset import Asset, normalize
from app.domain.severity import Severity, SeverityMapping


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Идентичность проблемы, п. 4.1 ТЗ: source_system + asset.name + monitor_name.

    Три слагаемых, и все три обязательны. Интеграция и объект — разные вещи
    (Zabbix прислал, скважина сломалась), и если их склеить, второе слагаемое
    перестанет нести информацию; monitor_name разделяет разные поломки одного
    объекта, иначе «диск заполнен» и «сервис недоступен» на одном хосте
    схлопнутся в одну проблему.

    Хранится как хэш, а не как конкатенация, по двум причинам. Значение получает
    фиксированную ширину, а это важно, потому что каждое входящее событие делает
    по нему индексный поиск (п. 2.3: до 50 событий/сек на пике). И хостнейм,
    содержащий символ-разделитель, больше не может подделать коллизию с другой
    тройкой.

    Обрати внимание, чего здесь намеренно нет: текста сообщения и source_event_id.
    «Диск заполнен на 94%» через минуту станет 95%, а Zabbix выдаёт новый id
    события на каждое срабатывание, — включение любого из них полностью убило бы
    дедупликацию.
    """

    value: str

    @classmethod
    def build(cls, source_system: str, asset_name: str, monitor_name: str) -> "Fingerprint":
        parts = (source_system, asset_name, monitor_name)
        # Без учёта регистра и пробелов: источники расходятся в том, зовётся ли
        # хост «DB-PROD-03» или «db-prod-03», и это не должно расщеплять одну
        # проблему на две. Символ \x1f (ASCII unit separator) в хостнейме
        # встретиться не может.
        canonical = "\x1f".join(normalize(part) for part in parts)
        return cls(hashlib.sha256(canonical.encode("utf-8")).hexdigest())


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    """Ровно payload из п. 2.1 ТЗ, до классификации.

    Отделён от `Event`, чтобы граница Шага 3 была сменой типа, а не заполнением
    nullable-поля: всё, что держит `Event`, гарантированно имеет платформенную
    severity, и ниже по стеку не нужны проверки `if severity is None`.

    `monitor_name` здесь обязателен, хотя п. 2.1 его не перечисляет, — см.
    docstring модуля `problem.py`.
    """

    source_system: str
    source_event_id: str
    monitor_name: str
    raw_severity: str
    asset: Asset
    message: str
    # Когда, по словам источника, это произошло, и когда нам об этом сообщили.
    # Расходятся при аварии самой интеграции: события копятся у неё и приезжают
    # пачкой с опозданием. Хранение по п. 7.2 должно считать срок от received_at,
    # а оператор смотрит на occurred_at.
    occurred_at: datetime
    received_at: datetime
    tags: frozenset[str] = field(default_factory=frozenset)

    def classify(self, mapping: SeverityMapping) -> "Event":
        """Весь Шаг 3 одним вызовом: проставить severity платформы и идентичность."""
        return Event(
            id=uuid.uuid4(),
            source_system=self.source_system,
            source_event_id=self.source_event_id,
            monitor_name=self.monitor_name,
            raw_severity=self.raw_severity,
            severity=mapping.classify(self.raw_severity),
            asset=self.asset,
            message=self.message,
            occurred_at=self.occurred_at,
            received_at=self.received_at,
            tags=self.tags,
            fingerprint=Fingerprint.build(
                self.source_system, self.asset.name, self.monitor_name
            ),
        )


@dataclass(frozen=True, slots=True)
class Event:
    """Классифицированная запись журнала — п. 4.3 ТЗ: «неизменяемая запись».

    `frozen=True` фиксирует это намерение в системе типов; слой хранения обязан
    его подкрепить, никогда не выполняя UPDATE по таблице событий. Ценность
    журнала для аудита в том, что можно воспроизвести, что именно платформе
    сообщили, — поэтому исправленное событие это *новое* событие, а не правка.
    """

    id: uuid.UUID
    source_system: str
    source_event_id: str
    monitor_name: str
    raw_severity: str
    severity: Severity
    asset: Asset
    message: str
    occurred_at: datetime
    received_at: datetime
    tags: frozenset[str]
    fingerprint: Fingerprint

    @property
    def is_recovery(self) -> bool:
        """Сигнал «объект вернулся в норму», закрывающий проблему (п. 4.3)."""
        return self.severity is Severity.OK
