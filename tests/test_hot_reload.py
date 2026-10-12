"""Тесты перечитывания изменившихся модулей: так демо подхватывает новый код без перезапуска."""

import os
import re
import sys
from pathlib import Path

import pytest

import hot_reload

NAMES = ["hr_base", "hr_user"]


@pytest.fixture
def modules(tmp_path, monkeypatch):
    """Два модуля: второй берёт значение из первого, как detectors из loader."""
    (tmp_path / "hr_base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "hr_user.py").write_text("from hr_base import VALUE\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in NAMES:
        sys.modules.pop(name, None)
        hot_reload._loaded_mtimes.pop(name, None)


def edit(path, text: str):
    """Новая версия файла, как после git pull: другое содержимое и более позднее время изменения."""
    path.write_text(text, encoding="utf-8")
    stat = path.stat()
    os.utime(path, (stat.st_atime, stat.st_mtime + 10))


def test_first_call_reloads_everything(modules):
    """Процесс мог загрузить модуль ещё до hot_reload, поэтому в первый раз перечитывается всё."""
    import hr_base  # загружен «старым процессом»
    edit(modules / "hr_base.py", "VALUE = 5\n")
    assert hr_base.VALUE == 1
    assert hot_reload.reload_changed(NAMES) == NAMES
    assert sys.modules["hr_base"].VALUE == 5
    assert hot_reload.reload_changed(NAMES) == []   # ничего не менялось - ничего не перечитывается


def test_changed_file_is_reloaded_with_dependents(modules):
    hot_reload.reload_changed(NAMES)
    edit(modules / "hr_base.py", "VALUE = 2\n")
    assert hot_reload.reload_changed(NAMES) == NAMES
    assert sys.modules["hr_base"].VALUE == 2
    assert sys.modules["hr_user"].VALUE == 2   # зависимый модуль тоже видит новое значение


def test_project_modules_are_listed_in_dependency_order():
    """Каждый модуль проекта стоит после тех, кого импортирует при загрузке.

    Импорт внутри функции (generator -> honest) выполняется при вызове и уже берёт новую версию.
    """
    order = {name: i for i, name in enumerate(hot_reload.PROJECT_MODULES)}
    root = Path(__file__).parent.parent
    for name in hot_reload.PROJECT_MODULES:
        source = (root / f"{name}.py").read_text(encoding="utf-8")
        for other in hot_reload.PROJECT_MODULES:
            if other != name and re.search(rf"^(import|from) {other}\b", source, re.MULTILINE):
                assert order[other] < order[name], f"{other} должен перечитываться раньше {name}"
