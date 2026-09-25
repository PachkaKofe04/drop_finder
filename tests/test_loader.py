"""Тесты загрузчика: синтетика и «реальные» выгрузки в разных форматах."""

import io
import math

import pandas as pd
import pytest

import loader
from loader import ATM, EXTERNAL, OTHER, load_ground_truth, load_transactions

# Выгрузка «как из банка»: Windows-1251, «;», русские колонки, полные номера карт,
# пустые получатели и отправители, неизвестный тип операции и две битые строки.
REAL_EXPORT = """Дата операции;Карта отправителя;Карта получателя;Сумма операции;Тип операции
25.09.2026 14:30;2202 2011 2345 4821;4276-1234-5678-9012;1 234,56 руб.;Перевод с карты на карту
25.09.2026 15:00;2202201123454821;;-5 000,00;Снятие наличных
26.09.2026 09:05;;4276123456789012;45000;Зачисление зарплаты
26.09.2026 10:00;;2202201123454821;3000;Пополнение наличными
2026-09-27 11:00:00;2202 9999 0000 4821;tok_abc123;700,5;Оплата услуг
не дата;2202201123454821;4276123456789012;100;Перевод
27.09.2026 12:00;2202201123454821;4276123456789012;abc;Перевод
"""
FULL_NUMBERS = ["2202201123454821", "4276123456789012", "2202999900004821"]


def as_file(text: str, encoding: str = "utf-8") -> io.BytesIO:
    """Текст как загруженный файл (так его передаёт Streamlit)."""
    return io.BytesIO(text.encode(encoding))


def parse_amount(text: str) -> float:
    return loader._parse_amounts(pd.Series([text], dtype="str"))[0]


@pytest.fixture
def real_export():
    return load_transactions(as_file(REAL_EXPORT, "cp1251"))


# --- Синтетика -------------------------------------------------------------------

def test_synthetic_roundtrip(synthetic, tmp_path):
    """Файл генератора читается без потерь и предупреждений."""
    tx, _ = synthetic
    path = tmp_path / "transactions.csv"
    tx.to_csv(path, index=False, float_format="%.2f", date_format="%Y-%m-%d %H:%M:%S")

    loaded, warnings = load_transactions(path)
    assert warnings == []
    pd.testing.assert_frame_equal(loaded, tx, check_dtype=False)


# --- Реальная выгрузка -----------------------------------------------------------

def test_real_export_rows(real_export):
    df, _ = real_export
    assert list(df["amount"]) == [1234.56, 5000.0, 45000.0, 3000.0, 700.5]
    assert list(df["op_type"]) == ["transfer", "cash_withdrawal", "top_up", "top_up", OTHER]
    assert df["datetime"].is_monotonic_increasing


def test_real_export_fills_atm_and_external(real_export):
    df, _ = real_export
    assert df.loc[1, "receiver_card"] == ATM
    assert df.loc[2, "sender_card"] == EXTERNAL


def test_real_export_reports_problems(real_export):
    _, warnings = real_export
    report = "\n".join(warnings)
    assert "Оплата услуг" in report
    assert "«дата не распознана»: 1" in report
    assert "«сумма не распознана или равна 0»: 1" in report
    assert "Всего отброшено строк: 2 из 7" in report


# --- Номера карт -------------------------------------------------------------------

def test_full_card_numbers_never_leave_loader(real_export):
    df, _ = real_export
    output = "".join(pd.concat([df["sender_card"], df["receiver_card"]])).replace(" ", "")
    for number in FULL_NUMBERS:
        assert number not in output


def test_same_card_in_different_spelling_gets_same_id():
    ids = {loader.normalize_card(v) for v in ["2202 2011 2345 4821", "2202-2011-2345-4821", "2202201123454821"]}
    assert len(ids) == 1


def test_cards_with_same_visible_digits_do_not_merge():
    first = loader.normalize_card("2202201123454821")
    second = loader.normalize_card("2202999900004821")
    assert first.startswith("2202 **** **** 4821 #")
    assert second.startswith("2202 **** **** 4821 #")
    assert first != second


@pytest.mark.parametrize("value", ["tok_abc123", "2202 **** **** 4821", ATM, EXTERNAL])
def test_non_card_numbers_are_kept(value):
    assert loader.normalize_card(value) == value


def test_secret_makes_tags_stable_between_runs(monkeypatch):
    monkeypatch.setenv("DROP_FINDER_SECRET", "test-secret")
    monkeypatch.setattr(loader, "_secret_key", None)
    first_run = loader.normalize_card("2202201123454821")

    monkeypatch.setattr(loader, "_secret_key", None)  # имитация нового запуска
    assert loader.normalize_card("2202201123454821") == first_run

    monkeypatch.setenv("DROP_FINDER_SECRET", "other-secret")
    monkeypatch.setattr(loader, "_secret_key", None)
    assert loader.normalize_card("2202201123454821") != first_run


def test_ground_truth_with_full_numbers_matches_transactions(real_export):
    df, _ = real_export
    gt = load_ground_truth(as_file("Номер карты;Схема\n2202 2011 2345 4821;transit\n", "cp1251"))
    assert gt.loc[0, "card"] == df.loc[0, "sender_card"]
    assert gt.loc[0, "scheme"] == "transit"


# --- Суммы, даты, типы операций ------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("1234,56", 1234.56),
    ("-250.5", 250.5),
    ("42", 42.0),
    ("5 000 ₽", 5000.0),
    ("12 500,00", 12500.0),  # неразрывный пробел между разрядами
    ("RUB 300", 300.0),
    ("-7 000,5 р.", 7000.5),
    ("100р", 100.0),
])
def test_amount_formats(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["1,234.56", "abc", "inf", "1e5", ""])
def test_ambiguous_or_broken_amounts_are_rejected(text):
    # «1,234.56» лучше отбросить, чем молча прочитать как 1.234
    assert math.isnan(parse_amount(text))


def test_mixed_date_formats_keep_day_and_month():
    """Основной формат русский, но ISO-дата 2026-09-05 не должна превратиться в 9 мая."""
    values = pd.Series(["05.09.2026 10:00"] * 5
                       + ["2026-09-05 11:00:00", "05.09.2026", "05.09.2026 12:30:15", "мусор"], dtype="str")
    parsed = loader._parse_datetime(values, None)
    assert parsed[5] == pd.Timestamp("2026-09-05 11:00:00")
    assert parsed[6] == pd.Timestamp("2026-09-05 00:00:00")
    assert parsed[7] == pd.Timestamp("2026-09-05 12:30:15")
    assert pd.isna(parsed[8])


@pytest.mark.parametrize("text, expected", [
    ("transfer", "transfer"),
    ("Перевод с карты на карту", "transfer"),
    ("P2P", "transfer"),
    ("Снятие наличных", "cash_withdrawal"),
    ("Выдача в банкомате", "cash_withdrawal"),
    ("ATM withdrawal", "cash_withdrawal"),
    ("Пополнение наличными", "top_up"),  # «наличные» не должны сделать это снятием
    ("Зачисление зарплаты", "top_up"),
    ("Оплата услуг", OTHER),
])
def test_operation_type_keywords(text, expected):
    assert loader._normalize_op_type(text) == expected


# --- Форматы файлов и ошибки ----------------------------------------------------

@pytest.mark.parametrize("separator", [",", ";", "\t"])
def test_separators(separator):
    header = separator.join(["datetime", "sender_card", "receiver_card", "amount", "op_type"])
    row = separator.join(["2026-08-01 10:00:00", "A", "B", "100", "transfer"])
    df, warnings = load_transactions(as_file(f"{header}\n{row}\n"))
    assert len(df) == 1 and warnings == []


def test_utf8_with_bom_and_russian_headers():
    text = "Дата;Отправитель;Получатель;Сумма;Тип\n01.08.2026 10:00;A;B;100;Перевод\n"
    df, _ = load_transactions(as_file(text, "utf-8-sig"))
    assert df.loc[0, "op_type"] == "transfer"
    assert df.loc[0, "datetime"] == pd.Timestamp("2026-08-01 10:00")


def test_column_map_and_datetime_format():
    text = "When,From,To,Value,Kind\n25/09/2026 14:30,A1,B2,100,transfer\n"
    column_map = {"datetime": "When", "sender_card": "From", "receiver_card": "To",
                  "amount": "Value", "op_type": "Kind"}
    df, _ = load_transactions(io.StringIO(text), column_map=column_map, datetime_format="%d/%m/%Y %H:%M")
    assert df.loc[0, "datetime"] == pd.Timestamp("2026-09-25 14:30")


def test_missing_columns_explain_how_to_fix():
    with pytest.raises(ValueError, match="column_map"):
        load_transactions(io.StringIO("When,From,To,Value,Kind\n"))


def test_file_without_valid_rows_is_rejected():
    text = "datetime,sender_card,receiver_card,amount,op_type\nмусор,A,B,abc,transfer\n"
    with pytest.raises(ValueError, match="не осталось"):
        load_transactions(io.StringIO(text))
