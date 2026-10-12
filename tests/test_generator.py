"""Тесты генератора: воспроизводимость, формат данных и каждая спрятанная схема."""

import hashlib
import random
import sys

import pandas as pd
import pytest

import generator
from loader import ATM, COLUMNS, EXTERNAL, load_ground_truth, load_transactions

HOUR = pd.Timedelta(hours=1)
MASKED_CARD = r"\d{4} \*{4} \*{4} \d{4}"

# Контрольная сумма простого режима, seed 42. На нём посчитаны цифры в README,
# поэтому простой режим меняется только осознанно - вместе с этой строкой и README.
EASY_SEED_42_SHA256 = "d67d42870f86d9794844b820600e18d58c983b20350926bd9fcd3266f1866d89"


def scheme_cards(ground_truth: pd.DataFrame, scheme_id: str) -> dict:
    """Роль -> карта для одного экземпляра схемы."""
    rows = ground_truth[ground_truth["scheme_id"] == scheme_id]
    return dict(zip(rows["role"], rows["card"], strict=True))


def withdrawals(tx: pd.DataFrame, card: str, start, hours: float) -> pd.Series:
    """Суммы снятий карты в интервале (start, start + hours]."""
    cash = tx[(tx["sender_card"] == card) & (tx["op_type"] == "cash_withdrawal")
              & (tx["datetime"] > start) & (tx["datetime"] <= start + hours * HOUR)]
    return cash["amount"]


@pytest.fixture(params=["synthetic", "synthetic_hard"])
def any_synthetic(request):
    """Общие свойства формата проверяются в обоих режимах."""
    return request.getfixturevalue(request.param)


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


def test_columns_and_period(any_synthetic):
    tx, _ = any_synthetic
    start = pd.Timestamp(generator.START_DATE)
    assert list(tx.columns) == COLUMNS
    assert tx["datetime"].min() >= start
    assert tx["datetime"].max() < start + pd.Timedelta(days=generator.DAYS)
    assert tx["datetime"].is_monotonic_increasing


def test_cards_are_masked(any_synthetic):
    tx, _ = any_synthetic
    cards = pd.concat([tx["sender_card"], tx["receiver_card"]])
    assert cards[~cards.isin([ATM, EXTERNAL])].str.fullmatch(MASKED_CARD).all()


def test_operation_types(any_synthetic):
    tx, _ = any_synthetic
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


# --- Сложный режим -------------------------------------------------------------

def test_easy_mode_is_frozen(synthetic):
    tx, gt = synthetic
    data = tx.to_csv(index=False, lineterminator="\n") + gt.to_csv(index=False, lineterminator="\n")
    assert hashlib.sha256(data.encode()).hexdigest() == EASY_SEED_42_SHA256


def test_hard_mode_is_easy_plus_extras(synthetic, synthetic_hard):
    """Все операции и дропы простого режима входят в сложный без изменений."""
    tx, gt = synthetic
    tx_hard, gt_hard = synthetic_hard
    assert len(tx_hard) > len(tx)
    assert (tx.merge(tx_hard, how="left", indicator=True)["_merge"] == "both").all()
    pd.testing.assert_frame_equal(gt_hard.head(len(gt)), gt)


def test_hard_mode_is_reproducible(synthetic_hard):
    tx, gt = generator.generate(42, 200, generator.HARD)
    pd.testing.assert_frame_equal(tx, synthetic_hard[0])
    pd.testing.assert_frame_equal(gt, synthetic_hard[1])


def test_unknown_difficulty():
    with pytest.raises(ValueError, match="сложность"):
        generator.generate(42, 200, "medium")


def test_hard_ground_truth(synthetic_hard):
    tx, gt = synthetic_hard
    assert gt["card"].is_unique
    assert gt.groupby("scheme")["scheme_id"].nunique().to_dict() == {
        "transit": generator.N_TRANSIT, "funnel": generator.N_FUNNEL, "fast_cashout": generator.N_FAST_CASHOUT,
        "split": generator.N_SPLIT, "warmup": generator.N_WARMUP, "p2p": generator.N_P2P,
    }
    # Обычные карты: 200 клиентов и вторые карты из ловушки «покупка со своей второй карты»
    cards = set(tx["sender_card"]) | set(tx["receiver_card"])
    assert len(cards - set(gt["card"]) - {ATM, EXTERNAL}) == 200 + 200 // 50


@pytest.mark.parametrize("scheme_id", [f"split_{i}" for i in range(1, generator.N_SPLIT + 1)])
def test_split(synthetic_hard, scheme_id):
    """Распределитель выжидает 7-24 часа и раздаёт деньги 3-4 дропам, те снимают с дневным лимитом."""
    tx, gt = synthetic_hard
    rows = gt[gt["scheme_id"] == scheme_id]
    hub = rows.loc[rows["role"] == "hub", "card"].item()
    branches = set(rows.loc[rows["role"] == "branch", "card"])
    assert 3 <= len(branches) <= 4

    incoming = tx[(tx["receiver_card"] == hub) & (tx["op_type"] == "transfer")]
    assert len(incoming) == 1
    victim_tx = incoming.iloc[0]

    parts = tx[(tx["sender_card"] == hub) & (tx["op_type"] == "transfer")]
    assert sorted(parts["receiver_card"]) == sorted(branches)
    delays = parts["datetime"] - victim_tx["datetime"]
    assert delays.min() >= 7 * HOUR and delays.max() <= 24 * HOUR
    assert 0.95 <= parts["amount"].sum() / victim_tx["amount"] <= 0.99 + 0.01

    for part in parts.itertuples():
        # За 6 часов снято не больше половины, за сутки с небольшим - почти всё
        assert withdrawals(tx, part.receiver_card, part.datetime, 6).sum() <= 0.5 * part.amount
        assert withdrawals(tx, part.receiver_card, part.datetime, 26).sum() >= 0.9 * part.amount


@pytest.mark.parametrize("scheme_id", [f"warmup_{i}" for i in range(1, generator.N_WARMUP + 1)])
def test_warmup(synthetic_hard, scheme_id):
    """Недели обычной жизни, потом крупное поступление и снятие с дневным лимитом."""
    tx, gt = synthetic_hard
    drop = scheme_cards(gt, scheme_id)["cashout"]
    ops = tx[(tx["sender_card"] == drop) | (tx["receiver_card"] == drop)]

    big = ops.loc[ops.loc[ops["receiver_card"] == drop, "amount"].idxmax()]
    assert big["amount"] >= 80_000
    assert big["datetime"] >= pd.Timestamp(generator.START_DATE) + pd.Timedelta(days=14)

    # До схемы карта живёт как обычная: доходы, переводы знакомым
    history = ops[ops["datetime"] < big["datetime"].normalize()]
    assert {"top_up", "transfer"} <= set(history["op_type"])
    assert (history["amount"] < 80_000).all()

    assert withdrawals(tx, drop, big["datetime"], 6).sum() <= 0.4 * big["amount"]
    assert 0.84 <= withdrawals(tx, drop, big["datetime"], 26).sum() / big["amount"] <= 0.95
    assert ops["datetime"].max() <= big["datetime"] + 26 * HOUR


@pytest.mark.parametrize("scheme_id", [f"p2p_{i}" for i in range(1, generator.N_P2P + 1)])
def test_p2p(synthetic_hard, scheme_id):
    """Покупатели платят по одному раз в 7-16 часов, суммы некруглые, накопленное регулярно снимается."""
    tx, gt = synthetic_hard
    drop = scheme_cards(gt, scheme_id)["receiver"]

    incoming = tx[(tx["receiver_card"] == drop) & (tx["op_type"] == "transfer")]
    assert len(incoming) >= 8
    assert incoming["sender_card"].is_unique
    gaps = incoming["datetime"].diff().dropna()
    assert gaps.min() >= 7 * HOUR and gaps.max() <= 16 * HOUR   # за любые 48 часов не больше 7
    assert (incoming["amount"] % 100 != 0).mean() > 0.5

    first, last = incoming["datetime"].min(), incoming["datetime"].max()
    cash = withdrawals(tx, drop, first, (last - first) / HOUR + 41)
    assert 0.85 <= cash.sum() / incoming["amount"].sum() <= 1


def test_own_card_payment_looks_like_transit():
    """Деньги приходят на вторую карту владельца и через 1-4 часа почти целиком уходят продавцу."""
    random.seed(0)
    cards = [f"card_{i}" for i in range(50)]
    first, second = pd.DataFrame(generator.own_card_payments(cards, count=1)).itertuples()

    assert first.sender_card in cards and first.receiver_card not in cards
    assert second.sender_card == first.receiver_card
    assert second.receiver_card in cards and second.receiver_card != first.sender_card
    assert HOUR <= second.datetime - first.datetime <= 4 * HOUR
    assert 0.95 <= second.amount / first.amount <= 1


def test_self_employed_looks_like_p2p():
    """Много поступлений от разных людей, но суммы круглые и клиенты платят повторно."""
    random.seed(0)
    cards = [f"card_{i}" for i in range(50)]
    ops = pd.DataFrame(generator.self_employed(cards, count=1))

    assert ops["receiver_card"].nunique() == 1
    assert 20 <= len(ops) <= 35
    assert 8 <= ops["sender_card"].nunique() <= 20
    assert ops["sender_card"].duplicated().any()
    assert (ops["amount"] % 500 == 0).all()


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


def test_cli_hard(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["generator.py", "--seed", "7", "--clients", "50", "--difficulty", "hard"])
    generator.main()

    gt = load_ground_truth(tmp_path / "ground_truth.csv")
    assert set(gt["scheme"]) == {"transit", "funnel", "fast_cashout", "split", "warmup", "p2p"}


def test_cli_rejects_too_few_clients(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["generator.py", "--clients", "10"])
    with pytest.raises(SystemExit):
        generator.main()
