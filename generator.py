"""
Генератор синтетических транзакций для детектора дропов.

Все данные выдуманы: номера карт случайные и маскированные,
совпадения с реальными картами случайны.

Запуск:
    python generator.py --seed 42
    python generator.py --seed 42 --difficulty hard

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

# Сложность синтетики. В простой схемы укладываются в пороги правил detectors.py.
# В сложной к ним добавлены схемы, которые эти пороги обходят, и ещё две ловушки.
EASY, HARD = "easy", "hard"
DIFFICULTIES = [EASY, HARD]
N_SPLIT = 2
N_WARMUP = 3
N_P2P = 2

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


def friend_transfers(card: str, friends: list, days: int = DAYS) -> list:
    """Переводы знакомым в первые days дней: скинуться на обед, вернуть долг, заплатить за аренду."""
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
        when = random_daytime(random.randrange(days))
        ops.append(tx(when, card, random.choice(friends), amount, "transfer"))
    return ops


def cash_withdrawals(card: str, days: int = DAYS) -> list:
    """Обычные снятия наличных в первые days дней: 0-5 раз, суммы кратны 500."""
    ops = []
    for _ in range(random.randint(0, 5)):
        amount = round_to(random.uniform(1000, 30000), 500)
        ops.append(tx(random_daytime(random.randrange(days), 7, 23), card, ATM, amount, "cash_withdrawal"))
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


def own_card_payments(cards: list, count: int) -> list:
    """Ловушка для детектора: крупная покупка со своей второй карты.

    Клиент переводит крупную сумму с основной карты на свою вторую (с кэшбэком
    или большим лимитом), и через 1-4 часа 95-100% суммы уходит продавцу.
    Вторая карта выглядит как транзитная, хотя все деньги принадлежат одному человеку.
    """
    ops = []
    for owner in random.sample(cards, count):
        second = new_card()
        seller = random.choice([c for c in cards if c != owner])
        when = random_daytime(random.randrange(DAYS - 1))
        amount = random.randint(20, 150) * 1000
        ops.append(tx(when, owner, second, amount, "transfer"))
        price = round_to(amount * random.uniform(0.95, 1.0), 100)
        ops.append(tx(after(when, 60, 240), second, seller, price, "transfer"))
    return ops


def self_employed(cards: list, count: int) -> list:
    """Ловушка для детектора: самозанятый (репетитор, мастер маникюра).

    Весь месяц получает оплату от многих разных людей, как P2P-дроп, но суммы
    круглые, постоянные клиенты платят повторно, а деньги остаются на карте.
    """
    ops = []
    for worker in random.sample(cards, count):
        customers = random.sample([c for c in cards if c != worker], random.randint(10, 20))
        price = random.choice([1500, 2000, 2500, 3000])
        for _ in range(random.randint(20, 35)):
            amount = price * random.choice([1, 1, 2])   # одно или два занятия
            when = random_daytime(random.randrange(DAYS))
            ops.append(tx(when, random.choice(customers), worker, amount, "transfer"))
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
            for when, w in zip(moments, weights, strict=True)]


def limited_cash_out(card: str, total: float, start: datetime) -> list:
    """Снимает total с дневным лимитом: сразу 20-40%, остальное на следующий день.

    Так выходит, когда банк или банкомат ограничивает сумму за сутки.
    За первые часы снимается меньше половины - на быстрое снятие это не похоже.
    """
    first = total * random.uniform(0.2, 0.4)
    ops = [tx(after(start, 5, 50), card, ATM, first // 100 * 100, "cash_withdrawal")]
    return ops + cash_out(card, total - first, after(start, 8 * 60, 24 * 60), 120)


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


# --- Схемы сложного режима: обходят пороги правил -------------------------------

def split_scheme(scheme_id: str, victim: str) -> tuple[list, list]:
    """Разветвление: потерпевший -> распределитель -> 3-4 дропа -> банкомат.

    Распределитель выжидает 7-24 часа («отлёжка») и раздаёт 95-99% суммы частями,
    получатели снимают свою часть с дневным лимитом. Ни одна карта не отдаёт
    90% за 6 часов и не снимает 80% за час.
    """
    hub = new_card()
    branches = [new_card() for _ in range(random.randint(3, 4))]

    t0 = random_moment(DAYS - 4)
    amount = random.randint(150, 400) * 1000
    ops = [tx(t0, victim, hub, amount, "transfer")]
    total = amount * random.uniform(0.95, 0.99)
    weights = [random.uniform(0.5, 1.0) for _ in branches]
    for branch, weight in zip(branches, weights, strict=True):
        when = after(t0, 7 * 60, 24 * 60)
        part = round(total * weight / sum(weights), 2)
        ops.append(tx(when, hub, branch, part, "transfer"))
        ops += limited_cash_out(branch, part * random.uniform(0.95, 0.99), when)
        ops += camouflage(branch)
    ops += camouflage(hub)

    truth = [drop_record(hub, "split", scheme_id, "hub")]
    truth += [drop_record(branch, "split", scheme_id, "branch") for branch in branches]
    return ops, truth


def warmup_scheme(scheme_id: str, victim: str, clients: list) -> tuple[list, list]:
    """Прогрев: 2-4 недели карта живёт обычной жизнью, чтобы у неё появилась история,
    потом на неё приходит крупная сумма и снимается с дневным лимитом.
    """
    drop = new_card()
    day = random.randint(14, DAYS - 3)

    # История до схемы: подработка, переводы знакомым, снятия
    ops = [tx(random_daytime(random.randrange(day)), EXTERNAL, drop, round_to(random.uniform(5000, 30000), 100),
              "top_up") for _ in range(random.randint(2, 4))]
    ops += friend_transfers(drop, random.sample(clients, random.randint(3, 6)), day)
    ops += cash_withdrawals(drop, day)

    t0 = day_start(day) + timedelta(seconds=random.randrange(24 * 3600))
    amount = random.randint(80, 300) * 1000
    if random.random() < 0.5:
        ops.append(tx(t0, victim, drop, amount, "transfer"))
    else:
        ops.append(tx(t0, EXTERNAL, drop, amount, "top_up"))
    ops += limited_cash_out(drop, amount * random.uniform(0.85, 0.95), t0)
    return ops, [drop_record(drop, "warmup", scheme_id, "cashout")]


def p2p_scheme(scheme_id: str, clients: list) -> tuple[list, list]:
    """P2P-дроп: «продаёт» криптовалюту на P2P-площадке, покупатели платят ему на карту.

    5-10 дней поступления по одному раз в 7-16 часов от разных людей, суммы
    некруглые (пересчёт по курсу). Раз в 20-40 часов 90-98% накопленного снимается.
    Поступления редкие: за любые 48 часов их не больше 7.
    """
    drop = new_card()

    start = random_moment(DAYS - 13)
    end = start + timedelta(days=random.randint(5, 10))
    times = [start]
    while times[-1] < end:
        times.append(after(times[-1], 7 * 60, 16 * 60))

    ops, balance = [], 0.0
    cash_at = after(start, 20 * 60, 40 * 60)
    for when, buyer in zip(times, random.sample(clients, len(times)), strict=True):
        while cash_at < when:
            if balance:
                ops += cash_out(drop, balance * random.uniform(0.9, 0.98), cash_at, 60)
                balance = 0.0
            cash_at = after(cash_at, 20 * 60, 40 * 60)
        amount = float(random.randint(3000, 30000))
        ops.append(tx(when, buyer, drop, amount, "transfer"))
        balance += amount
    ops += cash_out(drop, balance * random.uniform(0.9, 0.98), cash_at, 60)

    ops += camouflage(drop)
    return ops, [drop_record(drop, "p2p", scheme_id, "receiver")]


def hard_extras(clients: list, victims: list) -> tuple[list, list]:
    """Добавки сложного режима: схемы, обходящие пороги правил, и ещё две ловушки."""
    ops, truth = [], []
    for i in range(1, N_SPLIT + 1):
        scheme_ops, scheme_truth = split_scheme(f"split_{i}", victims.pop())
        ops += scheme_ops
        truth += scheme_truth

    for i in range(1, N_WARMUP + 1):
        scheme_ops, scheme_truth = warmup_scheme(f"warmup_{i}", victims.pop(), clients)
        ops += scheme_ops
        truth += scheme_truth

    for i in range(1, N_P2P + 1):
        scheme_ops, scheme_truth = p2p_scheme(f"p2p_{i}", clients)
        ops += scheme_ops
        truth += scheme_truth

    ops += own_card_payments(clients, max(1, len(clients) // 50))
    ops += self_employed(clients, max(1, len(clients) // 100))
    return ops, truth


# --- Сборка ------------------------------------------------------------------

def generate(seed: int, n_clients: int, difficulty: str = EASY) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Возвращает (transactions, ground_truth). Одинаковый seed - одинаковый результат.

    Сложный режим - это простой плюс добавки: они генерируются последними,
    поэтому все операции простого режима с тем же seed входят в сложный без изменений.
    """
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"Неизвестная сложность {difficulty!r}, допустимы: {', '.join(DIFFICULTIES)}")
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

    if difficulty == HARD:
        extra_ops, extra_truth = hard_extras(clients, victims)
        ops += extra_ops
        truth += extra_truth

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
    parser.add_argument("--difficulty", choices=DIFFICULTIES, default=EASY,
                        help="easy - схемы в пределах порогов правил, hard - ещё и схемы, "
                             "которые их обходят, и больше ловушек (по умолчанию easy)")
    args = parser.parse_args()
    if args.clients < 50:
        parser.error("--clients должно быть не меньше 50")

    transactions, ground_truth = generate(args.seed, args.clients, args.difficulty)

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
