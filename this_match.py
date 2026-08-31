"""Точка запуска сбора и выбора ближайшего матча.

Основная реализация сохранена в существующем модуле ``matches.py``, чтобы не
дублировать уже рабочую логику проекта.
"""

import asyncio

# Эти зависимости намеренно остаются частью публичной связки this_match/auth.
from auth import PROFILE_DIR, authorize  # noqa: F401
from matches import main


if __name__ == "__main__":
    asyncio.run(main())
