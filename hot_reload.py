"""
Перечитывание модулей проекта, изменившихся на диске после загрузки.

Streamlit Community Cloud после git pull заново выполняет app.py, но модули проекта,
уже загруженные в память, не перечитывает. Новый app.py тогда падает на импорте того,
чего нет в старой версии модуля, пока приложение не перезапустят вручную.

app.py вызывает reload_changed() первым делом, до импорта остальных модулей проекта.
При первом вызове в процессе перечитывается всё: процесс мог загрузить модули ещё до того,
как появился этот файл, и сравнить их не с чем. Дальше - только если файл изменился.
Сам этот модуль не перечитывается: он маленький и меняться не должен.
"""

import importlib
from pathlib import Path

# Порядок важен: модуль перечитывается после тех, от кого он зависит
PROJECT_MODULES = ["theme", "loader", "generator", "honest", "detectors", "metrics", "flow_graph", "tracing"]

_loaded_mtimes = {}   # модуль -> время изменения его файла, когда он был загружен


def _mtime(module) -> float:
    return Path(module.__file__).stat().st_mtime


def reload_changed(names=PROJECT_MODULES) -> list:
    """Загружает модули проекта; если файл хоть одного изменился с прошлой загрузки,
    перечитывает все (у остальных могут быть ссылки на старые объекты). Возвращает имена перечитанных."""
    modules = [importlib.import_module(name) for name in names]
    changed = any(_loaded_mtimes.get(module.__name__) != _mtime(module) for module in modules)
    if changed:
        modules = [importlib.reload(module) for module in modules]
    _loaded_mtimes.update({module.__name__: _mtime(module) for module in modules})
    return [module.__name__ for module in modules] if changed else []
