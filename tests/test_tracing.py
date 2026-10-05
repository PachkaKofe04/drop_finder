"""Тесты отслеживания денег."""

import pandas as pd
import pytest

from detectors import detect
from loader import ATM, COLUMNS, EXTERNAL
from tracing import CASHED, SETTLED, to_sankey, trace

T0 = pd.Timestamp("2026-08-10 10:00")


def ops(*rows) -> pd.DataFrame:
    """Операции из кортежей (минуты от T0, отправитель, получатель, сумма, тип)."""
    return pd.DataFrame([(T0 + pd.Timedelta(minutes=m), sender, receiver, float(amount), op_type)
                         for m, sender, receiver, amount, op_type in rows], columns=COLUMNS)


def settled(result) -> dict:
    return dict(zip(result.settled["card"], result.settled["amount"], strict=True))


def test_chain_to_atm():
    tx = ops(
        (0, "victim", "drop1", 100_000, "transfer"),
        (90, "drop1", "drop2", 98_500, "transfer"),
        (200, "drop2", ATM, 97_000, "cash_withdrawal"),
    )
    result = trace(tx, 0)
    assert result.cashed == 97_000
    assert settled(result) == {"drop1": 1_500, "drop2": 1_500}
    assert list(result.flows["receiver"]) == ["drop1", "drop2", ATM]
    assert result.cards == 2
    assert result.time_to_cash == pd.Timedelta(minutes=200)


def test_split_into_several_cards():
    tx = ops(
        (0, "victim", "drop", 100_000, "transfer"),
        (30, "drop", "a", 40_000, "transfer"),
        (60, "drop", "b", 55_000, "transfer"),
        (90, "a", ATM, 40_000, "cash_withdrawal"),
    )
    result = trace(tx, 0)
    assert result.cashed == 40_000
    assert settled(result) == {"b": 55_000, "drop": 5_000}


def test_traced_money_leaves_first_but_no_more_than_arrived():
    """Карта отправила больше, чем пришло: прослеживаем только пришедшее."""
    tx = ops((0, "victim", "drop", 100_000, "transfer"), (30, "drop", "b", 150_000, "transfer"))
    result = trace(tx, 0)
    assert result.flows.iloc[-1]["amount"] == 100_000
    assert settled(result) == {"b": 100_000}


def test_operations_before_arrival_are_ignored():
    tx = ops((0, "drop", "b", 50_000, "transfer"), (10, "victim", "drop", 100_000, "transfer"))
    result = trace(tx, 1)
    assert settled(result) == {"drop": 100_000}


def test_horizon_limits_the_search():
    tx = ops((0, "victim", "drop", 100_000, "transfer"), (80 * 60, "drop", ATM, 100_000, "cash_withdrawal"))
    assert trace(tx, 0).cashed == 0
    assert trace(tx, 0, horizon=pd.Timedelta(hours=96)).cashed == 100_000


def test_step_limit():
    tx = ops(
        (0, "victim", "a", 100_000, "transfer"),
        (10, "a", "b", 100_000, "transfer"),
        (20, "b", "c", 100_000, "transfer"),
    )
    result = trace(tx, 0, max_steps=2)
    assert list(result.flows["receiver"]) == ["a", "b"]
    assert settled(result) == {"b": 100_000}


def test_money_going_in_circles_is_not_counted_twice():
    rows = [(0, "victim", "a", 100_000, "transfer")]
    rows += [(10 * i, "a" if i % 2 else "b", "b" if i % 2 else "a", 100_000, "transfer") for i in range(1, 30)]
    result = trace(ops(*rows), 0, max_steps=50)
    assert result.cashed + result.settled_total == pytest.approx(100_000)
    assert result.flows["step"].max() <= 50


def test_start_from_top_up():
    tx = ops((0, EXTERNAL, "drop", 70_000, "top_up"), (10, "drop", ATM, 65_000, "cash_withdrawal"))
    result = trace(tx, 0)
    assert result.cashed == 65_000
    assert result.time_to_cash == pd.Timedelta(minutes=10)


def test_nothing_left_the_card():
    result = trace(ops((0, "victim", "drop", 100_000, "transfer")), 0)
    assert result.cashed == 0
    assert settled(result) == {"drop": 100_000}
    assert result.time_to_cash is None


def test_sankey_balances():
    tx = ops(
        (0, "victim", "drop1", 100_000, "transfer"),
        (90, "drop1", "drop2", 98_500, "transfer"),
        (200, "drop2", ATM, 97_000, "cash_withdrawal"),
    )
    sankey = to_sankey(trace(tx, 0), suspicious={"drop1", "drop2"}).data[0]
    labels = list(sankey.node.label)
    assert {"victim", "drop1", "drop2", CASHED, SETTLED} == set(labels)
    # В конечные узлы приходит ровно столько, сколько ушло от жертвы
    into_sinks = sum(v for t, v in zip(sankey.link.target, sankey.link.value, strict=True)
                     if labels[t] in (CASHED, SETTLED))
    assert into_sinks == pytest.approx(100_000)


def test_synthetic_victims(synthetic):
    """Деньги жертв доходят до банкомата, сумма всегда сходится."""
    transactions, truth = synthetic
    suspicious = set(detect(transactions)["card"])
    starts = transactions[transactions["receiver_card"].isin(suspicious)
                          & ~transactions["sender_card"].isin(suspicious)
                          & transactions["op_type"].isin(["transfer", "top_up"])]
    first_drops = set(truth.loc[truth["role"].isin(["drop1", "hub", "cashout"]), "card"])
    for idx, op in starts.iterrows():
        result = trace(transactions, idx)
        assert result.cashed + result.settled_total == pytest.approx(result.amount)
        if op["receiver_card"] in first_drops and op["amount"] >= 20_000:
            assert result.cashed >= 0.9 * result.amount


def test_operation_is_not_used_twice():
    """Деньги разошлись по двум картам и снова сошлись: снятие на 60 000 нельзя засчитать дважды."""
    tx = ops(
        (0, "victim", "drop", 100_000, "transfer"),
        (10, "drop", "a", 50_000, "transfer"),
        (20, "drop", "b", 50_000, "transfer"),
        (30, "a", "c", 50_000, "transfer"),
        (40, "b", "c", 50_000, "transfer"),
        (50, "c", ATM, 60_000, "cash_withdrawal"),
    )
    result = trace(tx, 0)
    assert result.cashed == 60_000
    assert settled(result) == {"c": 40_000}
