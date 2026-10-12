"""Тесты честных людей из сложного режима: каждая ситуация выглядит так, как описана в honest.py."""

import random

import pandas as pd
import pytest

import generator
import honest
from loader import ATM, EXTERNAL

HOUR = pd.Timedelta(hours=1)


@pytest.fixture(scope="module")
def data(synthetic_hard_labeled):
    tx, truth, lookalikes = synthetic_hard_labeled
    return tx, lookalikes


def people(data, situation: str) -> list:
    tx, lookalikes = data
    cards = list(lookalikes.loc[lookalikes["situation"] == situation, "card"])
    assert cards, situation
    return cards


def incoming(tx, card, op_type="transfer"):
    return tx[(tx["receiver_card"] == card) & (tx["op_type"] == op_type)]


def outgoing(tx, card, op_type=None):
    ops = tx[tx["sender_card"] == card]
    return ops if op_type is None else ops[ops["op_type"] == op_type]


def known_before(tx, a: str, b: str, when) -> bool:
    """Были ли операции между a и b раньше момента when."""
    pair = (((tx["sender_card"] == a) & (tx["receiver_card"] == b))
            | ((tx["sender_card"] == b) & (tx["receiver_card"] == a)))
    return bool((tx.loc[pair, "datetime"] < when).any())


def alone(situation: str, seed: int = 0) -> pd.DataFrame:
    """Операции одной ситуации без обычной жизни вокруг: так её проще проверить по описанию."""
    random.seed(seed)
    clients = [f"client_{i:02}" for i in range(80)]
    person = honest.Person("me", clients[:6], clients[6:])
    return pd.DataFrame(honest.SCENARIOS[situation](person))


def withdrawn(tx, card, start, hours) -> float:
    cash = outgoing(tx, card, "cash_withdrawal")
    return cash.loc[(cash["datetime"] > start) & (cash["datetime"] <= start + hours * HOUR), "amount"].sum()


# --- Общее ----------------------------------------------------------------------

def test_labels(synthetic_hard_labeled):
    tx, truth, lookalikes = synthetic_hard_labeled
    assert lookalikes["card"].is_unique
    assert not set(lookalikes["card"]) & set(truth["card"])
    assert set(lookalikes["card"]) <= set(tx["sender_card"]) | set(tx["receiver_card"])
    assert set(lookalikes["situation"]) == set(honest.SITUATIONS) | {"own_card", "self_employed"}
    counts = lookalikes["situation"].value_counts()
    assert counts[list(honest.SITUATIONS)].sum() == 200 // 5


def test_easy_mode_has_no_lookalikes():
    _, _, lookalikes = generator.generate_labeled(42, 60)
    assert lookalikes.empty and list(lookalikes.columns) == ["card", "situation"]


def test_everyone_has_ordinary_life(data):
    """Доход от организации, покупки, знакомые - как у обычного человека."""
    tx, lookalikes = data
    for card in people(data, "avito") + people(data, "mortgage"):
        income = incoming(tx, card, "top_up")
        assert len(income) >= 1
        assert income["sender_card"].str.startswith(("Работодатель", honest.PENSION_FUND)).all()
        assert outgoing(tx, card, "payment")["receiver_card"].isin(honest.SHOPS).sum() >= 5
        assert outgoing(tx, card, "transfer")["receiver_card"].nunique() >= 4


def test_salary_twice_a_month_pension_once(data):
    tx, _ = data
    for card in people(data, "salary_cash"):
        salary = incoming(tx, card, "top_up")
        assert len(salary) == 2 and salary["sender_card"].nunique() == 1
        assert salary["datetime"].diff().iloc[1] >= 14 * 24 * HOUR
    for card in people(data, "pension_cash"):
        pension = incoming(tx, card, "top_up")
        assert list(pension["sender_card"]) == [honest.PENSION_FUND]
        assert 15_000 <= pension["amount"].iloc[0] <= 45_000


# --- Деньги пришли и сразу ушли --------------------------------------------------

def test_mortgage(data):
    tx, _ = data
    for card in people(data, "mortgage"):
        payment = outgoing(tx, card, "payment").query("receiver_card == @honest.MORTGAGE_BANK").iloc[0]
        gift = incoming(tx, card).query("amount == @payment.amount").iloc[0]
        assert 30_000 <= payment["amount"] <= 70_000
        assert HOUR <= payment["datetime"] - gift["datetime"] <= 5 * HOUR
        assert known_before(tx, gift["sender_card"], card, gift["datetime"])   # супруг


def test_salary_and_pension_withdrawn_in_cash(data):
    tx, _ = data
    for card in people(data, "salary_cash") + people(data, "pension_cash"):
        for income in incoming(tx, card, "top_up").itertuples():
            assert withdrawn(tx, card, income.datetime, 1) >= 0.9 * income.amount


def test_deposit_move(data):
    tx, _ = data
    for card in people(data, "deposit_move"):
        big_in = incoming(tx, card).sort_values("amount").iloc[-1]
        big_out = outgoing(tx, card, "transfer").sort_values("amount").iloc[-1]
        assert big_in["amount"] >= 200_000 and big_out["amount"] == big_in["amount"]
        assert big_out["datetime"] - big_in["datetime"] <= 3 * HOUR
        # Обе карты свои: переводы с ними были и раньше
        assert known_before(tx, big_in["sender_card"], card, big_in["datetime"])
        assert known_before(tx, card, big_out["receiver_card"], big_in["datetime"])


def test_car_sale(data):
    tx, _ = data
    for card in people(data, "car_sale"):
        sale = incoming(tx, card).sort_values("amount").iloc[-1]
        dealer = outgoing(tx, card, "payment").query("receiver_card == @honest.CAR_DEALER").iloc[0]
        assert 500_000 <= sale["amount"] <= 2_000_000
        assert not known_before(tx, sale["sender_card"], card, sale["datetime"])   # покупатель - незнакомец
        assert dealer["datetime"] - sale["datetime"] <= 5 * HOUR
        assert 0.91 <= dealer["amount"] / sale["amount"] <= 1


def test_renovation(data):
    tx, _ = data
    for card in people(data, "renovation"):
        gift = incoming(tx, card).sort_values("amount").iloc[-1]
        builder = outgoing(tx, card, "transfer").sort_values("amount").iloc[-1]
        assert 150_000 <= gift["amount"] <= 400_000
        assert known_before(tx, gift["sender_card"], card, gift["datetime"])            # родители
        assert not known_before(tx, card, builder["receiver_card"], builder["datetime"])  # бригадир - новый
        assert builder["datetime"] - gift["datetime"] <= 4 * HOUR
        assert 0.89 <= builder["amount"] / gift["amount"] <= 1


# --- Много переводов от разных людей ---------------------------------------------

def test_avito(data):
    tx, _ = data
    for card in people(data, "avito"):
        sales = incoming(tx, card)
        strangers = [s for s, when in zip(sales["sender_card"], sales["datetime"], strict=True)
                     if not known_before(tx, s, card, when)]
        assert 4 <= len(strangers) and len(set(strangers)) == len(strangers)


def test_reseller(data):
    tx, _ = data
    for card in people(data, "reseller"):
        sales = incoming(tx, card).query("amount >= 15000")
        stock = outgoing(tx, card, "transfer").query("amount >= 10000")
        assert 10 <= len(sales) <= 22
        assert 2 <= stock["receiver_card"].nunique() <= 4
        assert 0.69 <= stock["amount"].sum() / sales["amount"].sum() <= 0.89


def test_taxi(data):
    tx, _ = data
    for card in people(data, "taxi"):
        fares = incoming(tx, card).query("amount <= 2200")
        per_day = fares.groupby(fares["datetime"].dt.date).size()
        assert 20 <= len(per_day) <= 25 and per_day.min() >= 6
        assert (outgoing(tx, card, "payment")["receiver_card"] == honest.GAS_STATION).sum() == len(per_day)


@pytest.mark.parametrize("seed", range(5))
def test_wedding(seed):
    """Гости дарят вечером в день свадьбы, на следующий день - ресторан и остаток себе на накопительный."""
    ops = alone("wedding", seed)
    received = ops[ops["receiver_card"] == "me"]
    wedding_day = received["datetime"].dt.date.value_counts().idxmax()
    gifts = received[received["datetime"].dt.date == wedding_day]
    assert 25 <= len(gifts) <= 60 and gifts["sender_card"].is_unique
    assert (gifts["datetime"].dt.hour >= 16).all()
    # Половина друзей среди гостей, и они знакомы с молодожёнами и до свадьбы
    friends = gifts["sender_card"].isin([f"client_{i:02}" for i in range(6)])
    assert friends.sum() == 3
    assert all(known_before(ops, friend, "me", gifts["datetime"].min()) for friend in gifts.loc[friends, "sender_card"])

    restaurant = ops[ops["receiver_card"] == honest.RESTAURANT].iloc[0]
    savings = ops[(ops["sender_card"] == "me") & (ops["op_type"] == "transfer")].sort_values("amount").iloc[-1]
    assert restaurant["datetime"].date() > gifts["datetime"].max().date()
    assert known_before(ops, "me", savings["receiver_card"], savings["datetime"])   # свой накопительный
    assert (restaurant["amount"] + savings["amount"]) / gifts["amount"].sum() <= 0.99


def test_fundraising(data):
    tx, _ = data
    for card in people(data, "fundraising"):
        clinic = outgoing(tx, card, "payment").query("receiver_card == @honest.CLINIC").iloc[0]
        donations = incoming(tx, card)
        donations = donations[(donations["datetime"] < clinic["datetime"])
                              & (donations["datetime"] > clinic["datetime"] - pd.Timedelta(days=5))
                              & donations["amount"].isin([300, 500, 1000, 2000, 3000, 5000, 10_000])]
        assert len(donations) >= 20
        assert 0.84 <= clinic["amount"] / donations["amount"].sum() <= 1.01


def test_joint_purchase_and_relocation(data):
    """Трудные случаи: много отправителей за 1-3 дня, потом почти всё уходит человеку или наличными."""
    tx, _ = data
    for card in people(data, "joint_purchase"):
        supplier = outgoing(tx, card, "transfer").sort_values("amount").iloc[-1]
        before = incoming(tx, card)
        before = before[(before["datetime"] < supplier["datetime"])
                        & (before["datetime"] > supplier["datetime"] - pd.Timedelta(days=3))]
        assert before["sender_card"].nunique() >= 10
        assert not known_before(tx, card, supplier["receiver_card"], supplier["datetime"])



@pytest.mark.parametrize("seed", range(5))
def test_relocation(seed):
    """Распродажа за 2-3 дня разным людям, потом почти всё снимается наличными за сутки."""
    ops = alone("relocation", seed)
    sales = ops[ops["receiver_card"] == "me"]
    assert 10 <= sales["sender_card"].nunique() == len(sales) <= 20
    assert sales["datetime"].max() - sales["datetime"].min() < 3 * 24 * HOUR
    assert withdrawn(ops, "me", sales["datetime"].max(), 24) >= 0.79 * sales["amount"].sum()


def test_job_loss(data):
    """Пришёл только аванс, друзья помогают, на аренду уходит больше любого одного перевода друга."""
    tx, _ = data
    for card in people(data, "job_loss"):
        assert len(incoming(tx, card, "top_up")) == 1
        rent = outgoing(tx, card, "transfer").sort_values("amount").iloc[-1]
        help_ = incoming(tx, card).query("amount >= 5000 & datetime < @rent.datetime")
        assert len(help_) >= 3
        assert all(known_before(tx, s, card, w) for s, w in zip(help_["sender_card"], help_["datetime"], strict=True))
        assert rent["amount"] > help_["amount"].max()


def test_organizations_are_not_cards(data):
    """Организации - названия, а не номера карт: ни ATM, ни EXTERNAL среди них нет."""
    tx, _ = data
    orgs = set(tx.loc[tx["op_type"] == "payment", "receiver_card"]) | {honest.PENSION_FUND}
    assert not orgs & {ATM, EXTERNAL}
    assert all("****" not in org for org in orgs)
