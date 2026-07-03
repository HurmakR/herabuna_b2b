"""
SQLite WAL-mode initializer.

Підключається до сигналу connection_created і вмикає WAL + оптимальні PRAGMA
для кожного нового з'єднання з SQLite.

WAL (Write-Ahead Logging) вирішує "database is locked":
- Читачі не блокують запис
- Запис не блокує читачів
- Тільки одночасні записувачі все ще чекають — але busy_timeout дає їм час

PRAGMA synchronous=NORMAL — безпечно і вдвічі швидше ніж FULL.
PRAGMA cache_size=-8000 — 8 МБ кешу в пам'яті (від'ємне = KB).
PRAGMA temp_store=MEMORY — тимчасові таблиці в RAM.
"""
from django.db.backends.signals import connection_created


def _configure_sqlite(sender, connection, **kwargs):
    if connection.vendor != 'sqlite':
        return
    cursor = connection.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    cursor.execute('PRAGMA synchronous=NORMAL')
    cursor.execute('PRAGMA busy_timeout=20000')
    cursor.execute('PRAGMA cache_size=-8000')
    cursor.execute('PRAGMA temp_store=MEMORY')
    cursor.execute('PRAGMA mmap_size=134217728')  # 128 MB memory-mapped I/O


def register():
    connection_created.connect(_configure_sqlite)
