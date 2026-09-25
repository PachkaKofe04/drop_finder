"""Тесты веб-интерфейса через AppTest: страница собирается без ошибок и реагирует на настройки."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).parent.parent / "app.py"
TABS = ["Подозрительные карты", "Разбор карты", "Граф связей", "Как это работает"]


@pytest.fixture
def app():
    return AppTest.from_file(str(APP), default_timeout=60).run()


def metrics(app: AppTest) -> dict:
    return {metric.label: metric.value for metric in app.metric}


def test_app_starts_with_synthetic_data(app):
    assert not app.exception
    assert app.title[0].value == "Детектор дропов"
    assert [tab.label for tab in app.tabs] == TABS
    assert metrics(app)["Подозрительных"] == "15"
    assert metrics(app)["Recall"] == "100.0%"
    assert len(app.dataframe[0].value) == 15


def test_demo_mode_offers_only_synthetic_data(app):
    """Без localhost (как в публичной демо-версии) загружать свои файлы нельзя."""
    assert app.sidebar.radio[0].options == ["Синтетика"]


def test_upload_can_be_allowed(monkeypatch):
    monkeypatch.setenv("DROP_FINDER_ALLOW_UPLOAD", "1")
    app = AppTest.from_file(str(APP), default_timeout=60).run()
    assert app.sidebar.radio[0].options == ["Синтетика", "Свой файл"]

    app.sidebar.radio[0].set_value("Свой файл").run()
    assert not app.exception
    assert "Загрузите CSV" in app.info[0].value


def test_stricter_rule_finds_fewer_cards(app):
    before = int(metrics(app)["Подозрительных"])
    transit_share = next(s for s in app.sidebar.slider if s.label.startswith("Ушло не меньше"))
    transit_share.set_value(100).run()
    assert not app.exception
    assert int(metrics(app)["Подозрительных"]) < before


def test_other_seed(app):
    seed = next(n for n in app.sidebar.number_input if n.label == "Seed")
    seed.set_value(7).run()
    assert not app.exception
    assert metrics(app)["Операций"] != "2 998"
