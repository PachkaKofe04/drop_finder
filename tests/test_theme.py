"""Тесты оформления: короткие подписи карт и цвета."""

import tomllib
from pathlib import Path

import theme


def test_short_card():
    assert theme.short_card("2202 **** **** 4821") == "•• 4821"
    assert theme.short_card("2202 **** **** 4821 #a1b2c3") == "•• 4821 #a1b2c3"
    assert theme.short_card("token-42") == "token-42"
    assert theme.short_card("customer-0000123456") == "•• 123456"
    assert theme.short_card("Работодатель 3") == "Работодатель 3"


def test_rgba():
    assert theme.rgba("#f07a86", 0.3) == "rgba(240, 122, 134, 0.3)"


def test_streamlit_theme_matches_palette():
    """Тема Streamlit задаётся отдельным файлом: фон, текст и акцент должны совпадать с theme.py."""
    config = tomllib.loads((Path(__file__).parent.parent / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["theme"]["backgroundColor"] == theme.BG
    assert config["theme"]["secondaryBackgroundColor"] == theme.SURFACE
    assert config["theme"]["textColor"] == theme.TEXT
    assert config["theme"]["primaryColor"] == theme.ACCENT
    assert config["theme"]["redColor"] == theme.RISK
