"""
Генератор синтетических транзакций для детектора дропов.

Все данные выдуманы: номера карт случайные и маскированные,
совпадения с реальными картами случайны.

Запуск:
    python generator.py --seed 42

Результат:
    data/transactions.csv  - все операции за 30 дней
    data/ground_truth.csv  - дроп-карты и схема, в которой они участвуют

Формат колонок общий с реальными данными, см. loader.py.
"""

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from loader import ATM, COLUMNS, EXTERNAL

DATA_DIR = Path(__file__).parent / "data"

# Начало периода зафиксировано, чтобы один и тот же seed давал одинаковый файл
START_DATE = datetime(2026, 8, 1)
DAYS = 30

# Сколько раз спрятать каждую схему
N_TRANSIT = 3
N_FUNNEL = 2
N_FAST_CASHOUT = 4

# Первые 4 цифры карт (платёжные системы, похожие на российские)
CARD_PREFIXES = ["2202", "2200", "4276", "4279", "5469", "5536"]

used_cards = set()  # чтобы номера карт не повторялись


# --- Вспомогательные функции -------------------------------------------------

def new_card() -> str:
    """Новый уникальный маскированный номер карты, например '2202 **** **** 4821'."""
    while True:
        card = f"{random.choice(CARD_PREFIXES)} **** **** {random.randint(0, 9999):04d}"
        if card not in used_cards:
            used_cards.add(card)
            return card


def tx(when, sender, receiver, amount, op_type) -> dict:
    """Одна строка будущего CSV."""
    return {"datetime": when, "sender_card": sender, "receiver_card": receiver,
            "amount": float(amount), "op_type": op_type}


def drop_record(card, scheme, scheme_id, role) -> dict:
    """Одна строка ground_truth.csv."""
    return {"card": card, "scheme": scheme, "scheme_id": scheme_id, "role": role}


def day_start(day: int) -> datetime:
    """Полночь дня с номером day (0 - первый день периода)."""
    return START_DATE + timedelta(days=day)


def random_daytime(day: int, start_hour=8, end_hour=23) -> datetime:
    """Случайный момент дня day в «человеческое» время (обычные клиенты ночью спят)."""
    seconds = random.randint(start_hour * 3600, end_hour * 3600 - 1)
    return day_start(day) + timedelta(seconds=seconds)


def random_moment(last_day: int) -> datetime:
    """Случайный момент в любое время суток в дни 0..last_day (мошенники работают и ночью)."""
    return day_start(random.randint(0, last_day)) + timedelta(seconds=random.randrange(24 * 3600))


def after(moment: datetime, min_minutes: float, max_minutes: float) -> datetime:
    """Момент через случайное число минут (от min_minutes до max_minutes) после moment."""
    return moment + timedelta(seconds=random.randint(int(min_minutes * 60), int(max_minutes * 60)))


def round_to(value: float, step: int) -> int:
    """Округление до шага: люди переводят «круглые» суммы, банкомат выдаёт кратно 100."""
    return max(step, round(value / step) * step)


# --- Обычные клиенты ---------------------------------------------------------

def income_ops(card: str) -> list:
    """Зарплата или другие поступления, иногда пополнение наличными."""
    ops = []
    if random.random() < 0.85:
        # Зарплата двумя частями (аванс 40% и остаток 60%) с разницей в 15 дней
        salary = random.randint(30, 150) * 1000
        first_day = random.randint(3, 12)
        for day, share in ((first_day, 0.4), (first_day + 15, 0.6)):
            when = random_daytime(day, 9, 18)
            amount = round(salary * share + random.uniform(-300, 300), 2)
            ops.append(tx(when, EXTERNAL, card, amount, "top_up"))
            # Ловушка для детектора: часть людей сразу снимает наличные с зарплаты.
            # Похоже на «быстрое снятие», но снимается лишь 10-40% суммы.
            if random.random() < 0.3:
                cash = round_to(amount * random.uniform(0.1, 0.4), 500)
                ops.append(tx(after(when, 20, 180), card, ATM, cash, "cash_withdrawal"))
    else:
        # Без зарплаты (пенсия, подработка, переводы из других банков)
        for _ in range(random.randint(2, 5)):
            amount = round_to(random.uniform(3000, 40000), 100)
            ops.append(tx(random_daytime(random.randrange(DAYS)), EXTERNAL, card, amount, "top_up"))

    # Изредка - пополнение наличными через банкомат
    for _ in range(random.choices([0, 1, 2], weights=[70, 25, 5])[0]):
        amount = round_to(random.uniform(1000, 20000), 500)
        ops.append(tx(random_daytime(random.randrange(DAYS)), EXTERNAL, card, amount, "top_up"))
    return ops


def friend_transfers(card: str, friends: list) -> list:
    """Переводы знакомым: скинуться на обед, вернуть долг, заплатить за аренду."""
    ops = []
    for _ in range(random.randint(2, 15)):
        kind = random.random()
        if kind < 0.70:
            amount = random.uniform(200, 3000)      # мелочь
        elif kind < 0.95:
            amount = random.uniform(3000, 15000)    # средние суммы
        else:
            amount = random.uniform(15000, 60000)   # крупные: аренда, долг
        # Чаще переводят круглые суммы, иногда точные («разделить счёт»)
        amount = round_to(amount, 100) if random.random() < 0.7 else round(amount, 2)
        when = random_daytime(random.randrange(DAYS))
        ops.append(tx(when, card, random.choice(friends), amount, "transfer"))
    return ops


def cash_withdrawals(card: str) -> list:
    """Обычные снятия наличных: 0-5 раз в месяц, суммы кратны 500."""
    ops = []
    for _ in range(random.randint(0, 5)):
        amount = round_to(random.uniform(1000, 30000), 500)
        ops.append(tx(random_daytime(random.randrange(DAYS), 7, 23), card, ATM, amount, "cash_withdrawal"))
    return ops


def group_collections(cards: list, count=2) -> list:
    """Ловушка для детектора: легальный сбор денег (подарок коллеге, складчина).

    Много разных карт переводят на одну за 1-2 дня - похоже на «воронку»,
    но суммы мелкие и одинаковые, а деньги никуда дальше не уходят.
    """
    ops = []
    for _ in range(count):
        organizer = random.choice(cards)
        payers = random.sample([c for c in cards if c != organizer], random.randint(8, 14))
        day = random.randrange(DAYS - 1)
        share = random.choice([500, 1000, 1500, 2000])
        for payer in payers:
            ops.append(tx(random_daytime(day + random.randint(0, 1)), payer, organizer, share, "transfer"))
    return ops


def generate_clients(n: int) -> tuple[list, list]:
    """Создаёт n обычных клиентов и их операции за период."""
    cards = [new_card() for _ in range(n)]

    # У каждого клиента свой круг общения из 3-8 знакомых
    friends = {}
    for card in cards:
        others = [c for c in cards if c != card]
        friends[card] = random.sample(others, random.randint(3, 8))

    ops = []
    for card in cards:
        ops += income_ops(card)
        ops += friend_transfers(card, friends[card])
        ops += cash_withdrawals(card)
    ops += group_collections(cards)
    return cards, ops


# --- Дроп-схемы --------------------------------------------------------------

def camouflage(card: str) -> list:
    """Немного обычной активности дроп-карты, чтобы вне схемы она не была пустой."""
    ops = []
    for _ in range(random.randint(1, 4)):
        when = random_daytime(random.randrange(DAYS))
        if random.random() < 0.5:
            ops.append(tx(when, EXTERNAL, card, round_to(random.uniform(500, 5000), 100), "top_up"))
        else:
            ops.append(tx(when, card, ATM, round_to(random.uniform(500, 3000), 100), "cash_withdrawal"))
    return ops


def cash_out(card: str, total: float, start: datetime, within_minutes: float) -> list:
    """Снимает total с карты 1-3 снятиями в течение within_minutes после start.

    Банкомат выдаёт суммы кратно 100, поэтому каждая часть округляется вниз.
    """
    parts = random.randint(1, 3)
    weights = [random.uniform(0.5, 1.0) for _ in range(parts)]
    moments = sorted(after(start, 0, within_minutes) for _ in range(parts))
    return [tx(when, card, ATM, total * w / sum(weights) // 100 * 100, "cash_withdrawal")
            for when, w in zip(moments, weights)]


def transit_scheme(scheme_id: str, victim: str) -> tuple[list, list]:
    """Транзит: потерпевший -> дроп1 -> дроп2 -> банкомат.

    Каждое звено через 1-3 часа, суммы почти равны
    (каждый дроп оставляет себе 0.5-3% за услуги).
    """
    drop1, drop2 = new_card(), new_card()

    t0 = random_moment(DAYS - 2)
    amount0 = random.randint(30, 200) * 1000  # потерпевший переводит круглую сумму
    t1 = after(t0, 60, 180)
    amount1 = round(amount0 * random.uniform(0.97, 0.995), 2)
    t2 = after(t1, 60, 180)
    amount2 = amount1 * random.uniform(0.97, 0.995) // 100 * 100

    ops = [
        tx(t0, victim, drop1, amount0, "transfer"),
        tx(t1, drop1, drop2, amount1, "transfer"),
        tx(t2, drop2, ATM, amount2, "cash_withdrawal"),
    ]
    ops += camouflage(drop1) + camouflage(drop2)
    truth = [drop_record(drop1, "transit", scheme_id, "drop1"),
             drop_record(drop2, "transit", scheme_id, "drop2")]
    return ops, truth


def funnel_scheme(scheme_id: str, victims: list) -> tuple[list, list]:
    """Воронка: 8-15 разных карт за 1-2 дня переводят на одну карту (hub),
    после чего hub пересылает почти всё следующему дропу, а тот обналичивает.
    """
    hub, next_drop = new_card(), new_card()

    t_start = random_moment(DAYS - 4)
    window_seconds = int(random.uniform(24, 48) * 3600)  # приток идёт 1-2 дня
    ops, total, last_income = [], 0.0, t_start
    for victim in victims:
        when = t_start + timedelta(seconds=random.randint(0, window_seconds))
        amount = round_to(random.uniform(3000, 50000), 100)
        ops.append(tx(when, victim, hub, amount, "transfer"))
        total += amount
        last_income = max(last_income, when)

    # Через 1-6 часов после последнего поступления hub пересылает 95-99% дальше
    t_out = after(last_income, 60, 360)
    forwarded = round(total * random.uniform(0.95, 0.99), 2)
    ops.append(tx(t_out, hub, next_drop, forwarded, "transfer"))
    # Следующий дроп обналичивает почти всё в течение нескольких часов
    ops += cash_out(next_drop, forwarded * random.uniform(0.95, 0.99), after(t_out, 30, 90), 180)

    ops += camouflage(hub) + camouflage(next_drop)
    truth = [drop_record(hub, "funnel", scheme_id, "hub"),
             drop_record(next_drop, "funnel", scheme_id, "next")]
    return ops, truth


def fast_cashout_scheme(scheme_id: str, victim: str) -> tuple[list, list]:
    """Быстрое снятие: карта получает крупную сумму и в течение часа снимает 90-99% её."""
    drop = new_card()

    t0 = random_moment(DAYS - 2)
    amount = random.randint(50, 300) * 1000
    if random.random() < 0.5:
        ops = [tx(t0, victim, drop, amount, "transfer")]   # перевод от потерпевшего
    else:
        ops = [tx(t0, EXTERNAL, drop, amount, "top_up")]   # деньги пришли из другого банка
    # Первое снятие через 3-10 минут, последнее - не позже часа после поступления
    ops += cash_out(drop, amount * random.uniform(0.9, 0.99), after(t0, 3, 10), 50)

    ops += camouflage(drop)
    truth = [drop_record(drop, "fast_cashout", scheme_id, "cashout")]
    return ops, truth


# --- Сборка ------------------------------------------------------------------

def generate(seed: int, n_clients: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Возвращает (transactions, ground_truth). Одинаковый seed - одинаковый результат."""
    random.seed(seed)
    used_cards.clear()

    clients, ops = generate_clients(n_clients)
    truth = []

    # Потерпевшие - обычные клиенты, каждый пострадал только один раз
    victims = random.sample(clients, len(clients))

    for i in range(1, N_TRANSIT + 1):
        scheme_ops, scheme_truth = transit_scheme(f"transit_{i}", victims.pop())
        ops += scheme_ops
        truth += scheme_truth

    for i in range(1, N_FUNNEL + 1):
        senders = [victims.pop() for _ in range(random.randint(8, 15))]
        scheme_ops, scheme_truth = funnel_scheme(f"funnel_{i}", senders)
        ops += scheme_ops
        truth += scheme_truth

    for i in range(1, N_FAST_CASHOUT + 1):
        scheme_ops, scheme_truth = fast_cashout_scheme(f"fast_cashout_{i}", victims.pop())
        ops += scheme_ops
        truth += scheme_truth

    transactions = (pd.DataFrame(ops, columns=COLUMNS)
                    .sort_values("datetime", kind="stable")
                    .reset_index(drop=True))
    return transactions, pd.DataFrame(truth)


def main():
    parser = argparse.ArgumentParser(description="Генератор синтетических транзакций с дроп-схемами")
    parser.add_argument("--seed", type=int, default=42,
                        help="зерно случайности: одинаковый seed - одинаковые данные (по умолчанию 42)")
    parser.add_argument("--clients", type=int, default=200,
                        help="число обычных клиентов, не меньше 50 (по умолчанию 200)")
    args = parser.parse_args()
    if args.clients < 50:
        parser.error("--clients должно быть не меньше 50")

    transactions, ground_truth = generate(args.seed, args.clients)

    DATA_DIR.mkdir(exist_ok=True)
    transactions.to_csv(DATA_DIR / "transactions.csv", index=False,
                        float_format="%.2f", date_format="%Y-%m-%d %H:%M:%S")
    ground_truth.to_csv(DATA_DIR / "ground_truth.csv", index=False)

    n_cards = pd.concat([transactions["sender_card"], transactions["receiver_card"]]).nunique() - 2
    print(f"Сохранено в {DATA_DIR}")
    print(f"  transactions.csv: {len(transactions)} операций, {n_cards} карт")
    print(f"  ground_truth.csv: {len(ground_truth)} дроп-карт")
    print(ground_truth["scheme"].value_counts().to_string())


if __name__ == "__main__":
    main()
