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


# --- Честные люди: оплаты и смягчающие обстоятельства -------------------------------

def events_of(tx: pd.DataFrame, card: str, rules: Rules | None = None) -> pd.DataFrame:
    events = find_events(tx, rules)
    return events[events["card"] == card]


def test_payment_is_not_passing_money_on():
    """Ипотека с перевода супруга: деньги ушли оплатой в банк - это трата, а не транзит."""
    paid = ops((0, "spouse", "card", 50_000, "transfer"), (120, "card", "Банк: ипотека", 50_000, "payment"))
    passed = ops((0, "spouse", "card", 50_000, "transfer"), (120, "card", "stranger", 50_000, "transfer"))
    assert flagged(paid, TRANSIT) == set()
    assert flagged(passed, TRANSIT) == {"card"}


def test_known_sender_mitigates_transit():
    """Родители уже переводили на эту карту - деньги на ремонт от них не делают карту транзитной."""
    tx = ops(
        (-5 * 24 * 60, "parent", "card", 3_000, "transfer"),
        (0, "parent", "card", 200_000, "transfer"),
        (120, "card", "builder", 195_000, "transfer"),
    )
    event = events_of(tx, "card").iloc[0]
    assert event["score"] == detectors.SCORES[TRANSIT] - detectors.MITIGATION
    assert event["mitigation"].startswith("смягчает: отправитель знаком с картой, операции между ними с 05.08")
    assert detect(tx).empty
    # Без смягчений - обычное срабатывание и никаких пояснений «смягчает», которые сбили бы аналитика
    unmitigated = events_of(tx, "card", Rules(mitigate=False)).iloc[0]
    assert unmitigated["score"] == detectors.SCORES[TRANSIT] and unmitigated["mitigation"] == ""
    assert set(detect(tx, Rules(mitigate=False))["card"]) == {"card"}


def test_known_suspicious_sender_does_not_mitigate():
    """Дропы заранее «познакомились»: знакомство с транзитной картой риск не снижает."""
    tx = ops(
        (-3 * 24 * 60, "drop1", "drop2", 1_000, "transfer"),
        (0, "victim", "drop1", 100_000, "transfer"),
        (90, "drop1", "drop2", 98_500, "transfer"),
        (200, "drop2", ATM, 97_000, "cash_withdrawal"),
    )
    assert (events_of(tx, "drop2")["mitigation"] == "").all()
    assert set(detect(tx)["card"]) == {"drop1", "drop2"}


def test_external_top_up_is_never_known():
    """Пополнения извне были и раньше, но EXTERNAL - не знакомый человек."""
    tx = ops(
        (-5 * 24 * 60, EXTERNAL, "drop", 2_000, "top_up"),
        (0, EXTERNAL, "drop", 120_000, "top_up"),
        (20, "drop", ATM, 115_000, "cash_withdrawal"),
    )
    assert (events_of(tx, "drop")["mitigation"] == "").all()
    assert set(detect(tx)["card"]) == {"drop"}


@pytest.mark.parametrize("employees, mitigated", [(3, True), (2, False)])
def test_salary_from_employer_of_many_mitigates_cashout(employees, mitigated):
    """Зарплата от работодателя, который платит многим, снятая целиком, - привычка, а не обналичивание."""
    rows = [(i, "Работодатель", f"colleague_{i}", 50_000, "top_up") for i in range(employees - 1)]
    rows += [(0, "Работодатель", "card", 60_000, "top_up"), (30, "card", ATM, 59_000, "cash_withdrawal")]
    event = events_of(ops(*rows), "card").iloc[0]
    assert event["rule"] == FAST_CASHOUT
    assert bool(event["mitigation"]) == mitigated
    if mitigated:
        assert "доход от «Работодатель», который платит 3 картам" in event["mitigation"]


def test_funnel_compares_outflow_with_everything_received():
    """Свадьба: 30 гостей по 5 000 ₽, потом половина ушла на накопительный - это не воронка.

    Окно, начатое с середины потока подарков, видит лишь часть денег - сравнивать надо со всем,
    что пришло за 48 часов до вывода.
    """
    gifts = funnel_income(30, minutes_between=12, amount=5_000, hub="couple")
    assert flagged(ops(*gifts, (12 * 60, "couple", "savings", 75_000, "transfer")), FUNNEL) == set()
    assert flagged(ops(*gifts, (12 * 60, "couple", "next", 145_000, "transfer")), FUNNEL) == {"couple"}


def test_funnel_from_known_senders_is_mitigated():
    """Деньги собрали в основном знакомые - смягчает, даже если всё ушло поставщику."""
    known = [(-24 * 60, f"victim_{i}", "hub", 500, "transfer") for i in range(6)]
    supplier = (12 * 60, "hub", "supplier", 78_000, "transfer")
    tx = ops(*known, *funnel_income(10, minutes_between=60, amount=8_000), supplier)
    event = events_of(tx, "hub").iloc[0]
    assert event["rule"] == FUNNEL
    # 6 знакомых по 8 000 ₽ из 83 000 ₽ за окно (первые 500 ₽ от них - ещё от незнакомых)
    assert "смягчает: 58% денег пришло от знакомых карты" in event["mitigation"]


def test_extra_rule_bonus_counts_only_strong_events():
    """Два смягчённых срабатывания не складываются в подозрение."""
    events = pd.DataFrame({
        "card": ["a", "a"], "rule": [TRANSIT, FAST_CASHOUT], "score": [25, 30],
        "time": [T0, T0], "amount": [1.0, 1.0], "reason": ["r1", "r2"], "mitigation": ["m", "m"],
    })
    assert detectors.summarize(events).empty
    assert detectors.summarize(events, min_risk=20)["risk"].iloc[0] == 35


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
