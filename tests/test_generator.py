"""Тесты генератора: воспроизводимость, формат данных и каждая спрятанная схема."""

import random
import sys

import pandas as pd
import pytest

import generator
from loader import ATM, COLUMNS, EXTERNAL, load_ground_truth, load_transactions

HOUR = pd.Timedelta(hours=1)
MASKED_CARD = r"\d{4} \*{4} \*{4} \d{4}"


def scheme_cards(ground_truth: pd.DataFrame, scheme_id: str) -> dict:
    """Роль -> карта для одного экземпляра схемы."""
    rows = ground_truth[ground_truth["scheme_id"] == scheme_id]
    return dict(zip(rows["role"], rows["card"]))


# --- Воспроизводимость и формат ----------------------------------------------

def test_same_seed_gives_same_data():
    tx1, gt1 = generator.generate(42, 200)
    tx2, gt2 = generator.generate(42, 200)
    pd.testing.assert_frame_equal(tx1, tx2)
    pd.testing.assert_frame_equal(gt1, gt2)


def test_different_seed_gives_different_data():
    tx1, _ = generator.generate(42, 200)
    tx2, _ = generator.generate(43, 200)
    assert not tx1.equals(tx2)


def test_columns_and_period(synthetic):
    tx, _ = synthetic
    start = pd.Timestamp(generator.START_DATE)
    assert list(tx.columns) == COLUMNS
    assert tx["datetime"].min() >= start
    assert tx["datetime"].max() < start + pd.Timedelta(days=generator.DAYS)
    assert tx["datetime"].is_monotonic_increasing


def test_cards_are_masked(synthetic):
    tx, _ = synthetic
    cards = pd.concat([tx["sender_card"], tx["receiver_card"]])
    assert cards[~cards.isin([ATM, EXTERNAL])].str.fullmatch(MASKED_CARD).all()


def test_operation_types(synthetic):
    tx, _ = synthetic
    assert set(tx["op_type"]) == {"transfer", "cash_withdrawal", "top_up"}
    # ATM - только получатель при снятии, EXTERNAL - только отправитель при пополнении
    assert ((tx["receiver_card"] == ATM) == (tx["op_type"] == "cash_withdrawal")).all()
    assert ((tx["sender_card"] == EXTERNAL) == (tx["op_type"] == "top_up")).all()
    assert (tx["amount"] > 0).all()


def test_regular_clients_count(synthetic):
    tx, gt = synthetic
    cards = set(tx["sender_card"]) | set(tx["receiver_card"])
    assert len(cards - set(gt["card"]) - {ATM, EXTERNAL}) == 200


def test_ground_truth(synthetic):
    tx, gt = synthetic
    assert gt["card"].is_unique
    assert gt["scheme"].value_counts().to_dict() == {
        "transit": 2 * generator.N_TRANSIT,
        "funnel": 2 * generator.N_FUNNEL,
        "fast_cashout": generator.N_FAST_CASHOUT,
    }
    assert set(gt["card"]) <= set(tx["sender_card"]) | set(tx["receiver_card"])


# --- Схемы ---------------------------------------------------------------------

@pytest.mark.parametrize("scheme_id", [f"transit_{i}" for i in range(1, generator.N_TRANSIT + 1)])
def test_transit(synthetic, scheme_id):
    """Потерпевший -> дроп1 -> дроп2 -> банкомат, звенья через 1-3 часа, суммы почти равны."""
    tx, gt = synthetic
    cards = scheme_cards(gt, scheme_id)
    drop1, drop2 = cards["drop1"], cards["drop2"]

    # Единственный перевод на дроп1 - от потерпевшего
    incoming = tx[(tx["receiver_card"] == drop1) & (tx["op_type"] == "transfer")]
    assert len(incoming) == 1
    victim_tx = incoming.iloc[0]

    hops = tx[(tx["sender_card"] == drop1) & (tx["receiver_card"] == drop2)]
    assert len(hops) == 1
    hop = hops.iloc[0]

    cash = tx[(tx["sender_card"] == drop2) & (tx["op_type"] == "cash_withdrawal")
              & (tx["datetime"] > hop["datetime"]) & (tx["datetime"] <= hop["datetime"] + 3 * HOUR)]
    withdrawal = cash.loc[cash["amount"].idxmax()]

    assert HOUR <= hop["datetime"] - victim_tx["datetime"] <= 3 * HOUR
    assert HOUR <= withdrawal["datetime"] - hop["datetime"] <= 3 * HOUR
    assert 0.97 <= hop["amount"] / victim_tx["amount"] < 1
    # Банкомат выдаёт кратно 100 ₽, поэтому допуск чуть шире
    assert 0.96 <= withdrawal["amount"] / hop["amount"] < 1


@pytest.mark.parametrize("scheme_id", [f"funnel_{i}" for i in range(1, generator.N_FUNNEL + 1)])
def test_funnel(synthetic, scheme_id):
    """8-15 разных карт за 1-2 дня переводят на одну, затем деньги уходят дальше."""
    tx, gt = synthetic
    cards = scheme_cards(gt, scheme_id)
    hub, next_drop = cards["hub"], cards["next"]

    incoming = tx[(tx["receiver_card"] == hub) & (tx["op_type"] == "transfer")]
    assert 8 <= incoming["sender_card"].nunique() <= 15
    assert incoming["datetime"].max() - incoming["datetime"].min() <= 48 * HOUR

    outgoing = tx[(tx["sender_card"] == hub) & (tx["op_type"] == "transfer")]
    assert len(outgoing) == 1
    out = outgoing.iloc[0]
    assert out["receiver_card"] == next_drop
    assert out["datetime"] > incoming["datetime"].max()
    assert 0.95 <= out["amount"] / incoming["amount"].sum() <= 0.99

    # Следующий дроп обналичивает почти всё в течение нескольких часов
    cash = tx[(tx["sender_card"] == next_drop) & (tx["op_type"] == "cash_withdrawal")
              & (tx["datetime"] > out["datetime"]) & (tx["datetime"] <= out["datetime"] + 5 * HOUR)]
    assert cash["amount"].sum() >= 0.9 * out["amount"]


@pytest.mark.parametrize("scheme_id", [f"fast_cashout_{i}" for i in range(1, generator.N_FAST_CASHOUT + 1)])
def test_fast_cashout(synthetic, scheme_id):
    """Карта получает крупную сумму и почти всю снимает в течение часа."""
    tx, gt = synthetic
    drop = scheme_cards(gt, scheme_id)["cashout"]

    incoming = tx[tx["receiver_card"] == drop]
    big = incoming.loc[incoming["amount"].idxmax()]
    assert big["amount"] >= 50_000

    cash = tx[(tx["sender_card"] == drop) & (tx["op_type"] == "cash_withdrawal")
              & (tx["datetime"] >= big["datetime"]) & (tx["datetime"] <= big["datetime"] + HOUR)]
    assert 0.89 <= cash["amount"].sum() / big["amount"] <= 1


# --- Ловушки для детектора -------------------------------------------------------

def test_group_collection_looks_like_funnel():
    """Складчина: 8-14 разных карт за 1-2 дня переводят одну и ту же мелкую сумму на одну карту."""
    random.seed(0)
    cards = [f"card_{i}" for i in range(50)]
    ops = pd.DataFrame(generator.group_collections(cards, count=1))

    assert ops["receiver_card"].nunique() == 1
    assert 8 <= ops["sender_card"].nunique() <= 14
    assert ops["amount"].nunique() == 1 and ops["amount"].iloc[0] <= 2000
    assert ops["datetime"].dt.normalize().nunique() <= 2


def test_regular_cards_have_no_fast_cashout(synthetic):
    """Снятие после зарплаты похоже на быстрое снятие, но не доходит до 90% суммы за час."""
    tx, gt = synthetic
    big = tx[(tx["amount"] >= 50_000) & (tx["op_type"] != "cash_withdrawal")
             & ~tx["receiver_card"].isin(gt["card"])]
    assert len(big) > 0
    for _, row in big.iterrows():
        cash = tx[(tx["sender_card"] == row["receiver_card"]) & (tx["op_type"] == "cash_withdrawal")
                  & (tx["datetime"] >= row["datetime"]) & (tx["datetime"] <= row["datetime"] + HOUR)]
        assert cash["amount"].sum() < 0.9 * row["amount"]


# --- Запуск из командной строки -----------------------------------------------

def test_cli_writes_files_readable_by_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["generator.py", "--seed", "7", "--clients", "60"])
    generator.main()

    tx, warnings = load_transactions(tmp_path / "transactions.csv")
    gt = load_ground_truth(tmp_path / "ground_truth.csv")
    assert warnings == []
    assert len(gt) == 2 * generator.N_TRANSIT + 2 * generator.N_FUNNEL + generator.N_FAST_CASHOUT
    assert set(gt["card"]) <= set(tx["sender_card"]) | set(tx["receiver_card"])


def test_cli_rejects_too_few_clients(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["generator.py", "--clients", "10"])
    with pytest.raises(SystemExit):
        generator.main()
