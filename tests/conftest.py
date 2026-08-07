"""Общая подготовка окружения для всего набора тестов.

`app.lib.config` создаёт `Settings` на импорте модуля, а `lib/auth.py` этот
конфиг импортирует — значит любой тест, который дотягивается до выдачи токенов,
требует валидных настроек ещё до первой строки самого теста. Переменные
окружения выставляются здесь, потому что pytest импортирует корневой conftest
раньше всего остального.

Переменные окружения имеют приоритет над `.env` в pydantic-settings, поэтому
локальный файл разработчика на тесты не влияет — и наоборот, тесты его не
портят.
"""

import os

os.environ.setdefault("SECRET_KEY", "x" * 64)
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("POSTGRES_DB", "test")
