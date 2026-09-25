"""Тесты правил на маленьких ручных примерах: где правило должно сработать и где нет."""

import sys

import pandas as pd
import pytest

import detectors
from detectors import EVENT_COLUMNS, FAST_CASHOUT, FUNNEL, SUMMARY_COLUMNS, TRANSIT, Rules, detect, find_events
from loader import ATM, COLUMNS, EXTERNAL

T0 = pd.Timestamp("2026-08-10 10:00")


def ops(*rows) -> pd.DataFrame:
    """Операции из кортежей (минуты от T0, отправитель, получатель, сумма, тип)."""
    return pd.DataFrame([(T0 + pd.Timedelta(minutes=m), sender, receiver, float(amount), op_type)
                         for m, sender, receiver, amount, op_type in rows], columns=COLUMNS)


def flagged(tx: pd.DataFrame, rule: str, rules: Rules | None = None) -> set:
    events = find_events(tx, rules)
    return set(events.loc[events["rule"] == rule, "card"])


def funnel_income(senders: int, minutes_between: int = 180, amount: float = 20_000, hub: str = "hub") -> list:
    """Переводы на hub от разных карт с равным шагом во времени."""
    return [(i * minutes_between, f"victim_{i}", hub, amount, "transfer") for i in range(senders)]


# --- Транзит ---------------------------------------------------------------------

def test_transit_chain():
    tx = ops(
        (0, "victim", "drop1", 100_000, "transfer"),
        (90, "drop1", "drop2", 98_500, "transfer"),
        (200, "drop2", ATM, 97_000, "cash_withdrawal"),
    )
    events = find_events(tx)
    transit = events[events["rule"] == TRANSIT]
    assert set(transit["card"]) == {"drop1", "drop2"}
    assert transit["reason"].str.contains("цепочка из 2 транзитных карт").all()
    assert "victim" not in set(events["card"])


def test_transit_through_several_payments():
    """Деньги ушли не одним платежом, а несколькими - это тоже транзит."""
    tx = ops(
        (0, "victim", "drop", 100_000, "transfer"),
        (30, "drop", "a", 40_000, "transfer"),
        (60, "drop", "b", 55_000, "transfer"),
    )
    assert flagged(tx, TRANSIT) == {"drop"}


@pytest.mark.parametrize("rows", [
    pytest.param([(60, "card", "b", 50_000, "transfer")], id="ушла только половина"),
    pytest.param([(7 * 60, "card", "b", 99_000, "transfer")], id="ушло позже 6 часов"),
    pytest.param([(60, "card", "b", 125_000, "transfer")], id="ушло больше, чем пришло"),
])
def test_not_transit(rows):
    tx = ops((0, "friend", "card", 100_000, "transfer"), *rows)
    assert flagged(tx, TRANSIT) == set()


def test_small_amounts_are_not_transit():
    tx = ops((0, "friend", "card", 5_000, "transfer"), (30, "card", "b", 4_900, "transfer"))
    assert flagged(tx, TRANSIT) == set()


def test_transit_window_is_configurable():
    tx = ops((0, "victim", "drop", 100_000, "transfer"), (7 * 60, "drop", "b", 99_000, "transfer"))
    assert flagged(tx, TRANSIT, Rules(transit_window=pd.Timedelta(hours=8))) == {"drop"}


# --- Воронка -----------------------------------------------------------------------

def test_funnel():
    income = funnel_income(10)  # 10 отправителей за 27 часов, всего 200 000 ₽
    tx = ops(*income, (income[-1][0] + 120, "hub", "next", 190_000, "transfer"))
    assert flagged(tx, FUNNEL) == {"hub"}


def test_group_collection_is_not_funnel():
    """Складчина: много мелких переводов, деньги остаются на карте."""
    tx = ops(*funnel_income(12, amount=1_000))
    assert flagged(tx, FUNNEL) == set()


@pytest.mark.parametrize("income, forwarded", [
    pytest.param(funnel_income(10), 50_000, id="ушла только четверть"),
    pytest.param(funnel_income(10, minutes_between=8 * 60), 190_000, id="приток растянут на 3 дня"),
    pytest.param(funnel_income(6, amount=35_000), 200_000, id="мало отправителей"),
])
def test_not_funnel(income, forwarded):
    tx = ops(*income, (income[-1][0] + 120, "hub", "next", forwarded, "transfer"))
    assert flagged(tx, FUNNEL) == set()


def test_many_transfers_from_one_sender_are_not_funnel():
    income = [(i * 60, "same_sender", "hub", 20_000, "transfer") for i in range(10)]
    tx = ops(*income, (12 * 60, "hub", "next", 190_000, "transfer"))
    assert flagged(tx, FUNNEL) == set()


# --- Быстрое обналичивание ----------------------------------------------------------

def test_fast_cashout():
    tx = ops(
        (0, EXTERNAL, "drop", 70_000, "top_up"),
        (10, "drop", ATM, 40_000, "cash_withdrawal"),
        (40, "drop", ATM, 25_000, "cash_withdrawal"),
    )
    assert flagged(tx, FAST_CASHOUT) == {"drop"}


@pytest.mark.parametrize("rows", [
    pytest.param([(0, EXTERNAL, "card", 60_000, "top_up"), (30, "card", ATM, 15_000, "cash_withdrawal")],
                 id="сняли часть зарплаты"),
    pytest.param([(0, EXTERNAL, "card", 70_000, "top_up"), (90, "card", ATM, 65_000, "cash_withdrawal")],
                 id="сняли через полтора часа"),
    pytest.param([(0, EXTERNAL, "card", 20_000, "top_up"), (10, "card", ATM, 20_000, "cash_withdrawal")],
                 id="небольшая сумма"),
])
def test_not_fast_cashout(rows):
    assert flagged(ops(*rows), FAST_CASHOUT) == set()


# --- Итоговый список ----------------------------------------------------------------

def test_card_matching_two_rules_gets_higher_risk():
    """Пришёл перевод и почти целиком снят за полчаса: это и транзит, и быстрое обналичивание."""
    tx = ops((0, "victim", "drop", 100_000, "transfer"), (30, "drop", ATM, 95_000, "cash_withdrawal"))
    result = detect(tx)
    assert list(result["card"]) == ["drop"]
    assert result.loc[0, "rules"] == "transit, fast_cashout"
    assert result.loc[0, "risk"] > max(find_events(tx)["score"])


def test_summary_one_row_per_card_sorted_by_risk():
    tx = ops(
        (0, "victim", "drop1", 100_000, "transfer"),
        (90, "drop1", "drop2", 98_500, "transfer"),
        (200, "drop2", ATM, 97_000, "cash_withdrawal"),
        (0, EXTERNAL, "drop3", 70_000, "top_up"),
        (10, "drop3", ATM, 65_000, "cash_withdrawal"),
    )
    result = detect(tx)
    assert list(result.columns) == SUMMARY_COLUMNS
    assert set(result["card"]) == {"drop1", "drop2", "drop3"}
    assert result["card"].is_unique
    assert result["risk"].is_monotonic_decreasing
    assert result["reason"].str.len().gt(0).all()


def test_regular_activity_gives_empty_result():
    tx = ops(
        (0, EXTERNAL, "alice", 80_000, "top_up"),
        (120, "alice", "bob", 1_500, "transfer"),
        (600, "alice", ATM, 5_000, "cash_withdrawal"),
    )
    result = detect(tx)
    assert result.empty
    assert list(result.columns) == SUMMARY_COLUMNS


def test_empty_input():
    assert list(find_events(ops()).columns) == EVENT_COLUMNS
    assert detect(ops()).empty


# --- Запуск из командной строки -----------------------------------------------

def test_cli(synthetic, tmp_path, monkeypatch, capsys):
    transactions, truth = synthetic
    transactions.to_csv(tmp_path / "tx.csv", index=False)
    truth.to_csv(tmp_path / "truth.csv", index=False)
    out = tmp_path / "suspicious.csv"
    monkeypatch.setattr(sys, "argv", ["detectors.py", str(tmp_path / "tx.csv"),
                                      "--truth", str(tmp_path / "truth.csv"), "--out", str(out)])
    detectors.main()

    printed = capsys.readouterr().out
    assert "Подозрительных карт" in printed and "Recall: 100.0%" in printed
    assert list(pd.read_csv(out).columns) == SUMMARY_COLUMNS
