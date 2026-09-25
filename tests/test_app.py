"""Интерфейс запускается без ошибок."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).parent.parent / "app.py"


def test_app_starts():
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.title[0].value == "Детектор дропов"
