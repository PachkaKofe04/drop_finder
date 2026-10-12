"""
Честные люди в ситуациях, похожих на схемы дропов. Только для сложного режима генератора.

Каждый такой человек - новая карта с обычной жизнью (зарплата или пенсия, переводы
знакомым, покупки, снятия) и одной ситуацией: ипотека, продажа машины, свадьба, сбор
на лечение... Разметка говорит, кто в какой ситуации: по ней видно, кого детектор
принимает за дропа по ошибке.

Параметры взяты из открытых данных: средняя пенсия около 25 тыс. ₽, средняя зарплата
около 100 тыс. ₽ и по закону выплачивается не реже раза в полмесяца, друзья дарят
на свадьбу 5-20 тыс. ₽, родители - от 50 тыс. ₽.
"""

import random
from dataclasses import dataclass, field

from generator import DAYS, after, cash_out, cash_withdrawals, day_start, new_card, random_daytime, round_to, tx

# Организации: платят зарплату и пенсию, получают оплату. Это не карты, а названия,
# как в реальной выписке («Соцфонд», «Автосалон»).
PENSION_FUND = "Соцфонд"
MORTGAGE_BANK = "Банк: ипотека"
CAR_DEALER = "Автосалон"
RESTAURANT = "Ресторан"
CLINIC = "Клиника"
GAS_STATION = "АЗС"
SHOPS = [f"Магазин {i}" for i in range(1, 13)]

SITUATIONS = {
    "mortgage": "Ипотека с перевода супруга",
    "salary_cash": "Вся зарплата наличными",
    "pension_cash": "Пенсия наличными",
    "deposit_move": "Перекладывает вклад между своими банками",
    "car_sale": "Продал машину, купил новую",
    "renovation": "Ремонт на деньги родителей",
    "avito": "Продаёт вещи на Авито",
    "reseller": "Перекупщик телефонов",
    "taxi": "Таксист, оплата переводом",
    "wedding": "Свадьба, подарки переводом",
    "fundraising": "Сбор на лечение",
    "joint_purchase": "Организатор совместной покупки",
    "relocation": "Распродажа перед переездом",
    "job_loss": "Потерял работу, помогают друзья",
}
# Названия для отчётов: ситуации отсюда и две ловушки из generator.py
TITLES = {
    **SITUATIONS,
    "own_card": "Покупка со своей второй карты",
    "self_employed": "Самозанятый (репетитор)",
}


@dataclass
class Person:
    card: str
    friends: list            # знакомые: переводы с ними были и раньше
    strangers: list          # все остальные клиенты
    incomes: list = field(default_factory=list)   # (когда, сколько) - зарплата или пенсия


def history(sender: str, receiver: str, count: int, last_day: int) -> list:
    """Несколько небольших переводов до события: эти двое уже знакомы."""
    return [tx(random_daytime(random.randrange(last_day)), sender, receiver,
               round_to(random.uniform(500, 8000), 100), "transfer") for _ in range(count)]


def base_life(person: Person, payer: str, pension: bool, salary_parts: int = 2, min_salary: int = 45) -> list:
    """Обычная жизнь: доход, переводы знакомым в обе стороны, покупки, снятия.

    salary_parts - сколько частей зарплаты пришло (1 - только аванс), min_salary - в тысячах ₽.
    """
    card, ops = person.card, []
    if pension:
        # Пенсия раз в месяц в один и тот же день, утром; в среднем около 25 тыс. ₽
        amount = round(random.triangular(15_000, 45_000, 22_000), 2)
        person.incomes.append((random_daytime(random.randint(3, 20), 7, 11), amount))
    else:
        # Зарплата двумя частями с разницей в 15 дней (аванс 40%, остаток 60%)
        salary = random.randint(min_salary, 180) * 1000
        first_day = random.randint(3, 12)
        for part in range(salary_parts):
            share = 0.4 if part == 0 else 0.6
            person.incomes.append((random_daytime(first_day + 15 * part, 9, 18),
                                   round(salary * share + random.uniform(-300, 300), 2)))
    ops += [tx(when, payer, card, amount, "top_up") for when, amount in person.incomes]

    for friend in person.friends:
        ops += history(card, friend, random.randint(1, 3), DAYS)
        if random.random() < 0.5:
            ops += history(friend, card, random.randint(1, 2), DAYS)
    for _ in range(random.randint(5, 15)):
        ops.append(tx(random_daytime(random.randrange(DAYS), 8, 22), card, random.choice(SHOPS),
                      round(random.uniform(300, 6000), 2), "payment"))
    return ops + cash_withdrawals(card)


# --- Деньги пришли и сразу ушли --------------------------------------------------

def mortgage(person: Person) -> list:
    """Супруг переводит сумму платежа по ипотеке, через 1-5 часов она уходит в банк."""
    spouse = random.choice(person.friends)
    day = random.randint(12, DAYS - 3)
    installment = random.randint(30, 70) * 1000
    when = random_daytime(day, 9, 19)
    return history(spouse, person.card, random.randint(2, 4), day) + [
        tx(when, spouse, person.card, installment, "transfer"),
        tx(after(when, 60, 300), person.card, MORTGAGE_BANK, installment, "payment"),
    ]


def salary_cash(person: Person) -> list:
    """В день зарплаты снимает её почти целиком наличными, в течение часа."""
    return [op for when, amount in person.incomes
            for op in cash_out(person.card, amount * random.uniform(0.92, 1.0), after(when, 10, 30), 30)]


def pension_cash(person: Person) -> list:
    """Пенсионер в день пенсии снимает её всю наличными."""
    when, amount = person.incomes[0]
    return cash_out(person.card, amount * random.uniform(0.95, 1.0), after(when, 15, 40), 15)


def deposit_move(person: Person) -> list:
    """Переводит вклад из одного своего банка в другой, где ставка выше, через эту карту."""
    own_from, own_to = new_card(), new_card()
    day = random.randint(10, DAYS - 3)
    amount = random.randint(20, 150) * 10_000
    when = random_daytime(day, 9, 21)
    return (history(own_from, person.card, random.randint(2, 3), day)
            + history(person.card, own_to, random.randint(2, 3), day)
            + [tx(when, own_from, person.card, amount, "transfer"),
               tx(after(when, 20, 180), person.card, own_to, amount, "transfer")])


def car_sale(person: Person) -> list:
    """Покупатель платит за машину, в тот же день почти вся сумма уходит в автосалон за новую."""
    buyer = random.choice(person.strangers)
    when = random_daytime(random.randint(5, DAYS - 3), 10, 17)
    price = random.randint(50, 200) * 10_000
    return [tx(when, buyer, person.card, price, "transfer"),
            tx(after(when, 60, 300), person.card, CAR_DEALER, round_to(price * random.uniform(0.92, 1.0), 1000),
               "payment")]


def renovation(person: Person) -> list:
    """Родители дают деньги на ремонт, в тот же день они уходят бригадиру."""
    parent, builder = random.choice(person.friends), new_card()
    day = random.randint(10, DAYS - 3)
    amount = random.randint(15, 40) * 10_000
    when = random_daytime(day, 9, 18)
    return history(parent, person.card, random.randint(2, 3), day) + [
        tx(when, parent, person.card, amount, "transfer"),
        tx(after(when, 60, 240), person.card, builder, round_to(amount * random.uniform(0.9, 1.0), 1000), "transfer"),
    ]


# --- Много переводов от разных людей ---------------------------------------------

def avito(person: Person) -> list:
    """Иногда продаёт вещи: несколько покупателей за месяц, деньги остаются на карте."""
    buyers = random.sample(person.strangers, random.randint(4, 12))
    return [tx(random_daytime(random.randrange(DAYS)), buyer, person.card,
               round_to(random.uniform(800, 30_000), 100), "transfer") for buyer in buyers]


def reseller(person: Person) -> list:
    """Перепродаёт телефоны: после каждой продажи покупает новый у поставщика за 70-88% цены."""
    suppliers = [new_card() for _ in range(random.randint(2, 4))]
    ops = []
    for _ in range(random.randint(10, 22)):
        when = random_daytime(random.randrange(DAYS - 2))
        price = round_to(random.uniform(15_000, 60_000), 500)
        ops.append(tx(when, random.choice(person.strangers), person.card, price, "transfer"))
        ops.append(tx(after(when, 30, 30 * 60), person.card, random.choice(suppliers),
                      round_to(price * random.uniform(0.7, 0.88), 500), "transfer"))
    return ops


def taxi(person: Person) -> list:
    """Таксист: часть пассажиров платит переводом - по 6-14 мелких поступлений за смену.

    Деньги тратятся на бензин, раз в неделю часть уходит супруге, раз в неделю - наличные.
    """
    ops = []
    spouse = random.choice(person.friends)
    for day in sorted(random.sample(range(DAYS), random.randint(20, 25))):
        for _ in range(random.randint(6, 14)):
            ops.append(tx(random_daytime(day, 7, 23), random.choice(person.strangers), person.card,
                          float(random.randint(250, 2200)), "transfer"))
        ops.append(tx(random_daytime(day, 7, 23), person.card, GAS_STATION, round(random.uniform(1500, 3500), 2),
                      "payment"))
        if day % 7 == 6:
            ops.append(tx(random_daytime(day, 20, 23), person.card, spouse, random.randint(10, 25) * 1000, "transfer"))
            ops += cash_out(person.card, random.randint(5, 15) * 1000, random_daytime(day, 18, 22), 30)
    return ops


def wedding(person: Person) -> list:
    """Гости дарят деньги переводом, на следующий день - доплата ресторану и остаток себе на накопительный."""
    savings = new_card()
    day = random.randint(10, DAYS - 3)
    guests = random.sample(person.friends, len(person.friends) // 2)
    ops = history(person.card, savings, random.randint(2, 3), day)
    for friend in guests:
        ops += history(friend, person.card, 1, day)
    guests += random.sample(person.strangers, min(random.randint(25, 60) - len(guests), len(person.strangers)))
    total = 0.0
    for guest in guests:
        amount = random.randint(3, 15) * 1000 if random.random() < 0.7 else random.randint(4, 12) * 5000
        ops.append(tx(random_daytime(day, 16, 24), guest, person.card, amount, "transfer"))
        total += amount
    next_day = random_daytime(day + 1, 12, 18)
    restaurant = random.uniform(0.35, 0.5)
    ops.append(tx(next_day, person.card, RESTAURANT, round_to(total * restaurant, 1000), "payment"))
    ops.append(tx(after(next_day, 30, 180), person.card, savings,
                  round_to(total * random.uniform(0.4, 0.98 - restaurant), 1000), "transfer"))
    return ops


def fundraising(person: Person) -> list:
    """Сбор на лечение: незнакомые люди за 2-4 дня присылают кто сколько может, потом оплата клиники."""
    start = random.randint(5, DAYS - 6)
    ops, total, last = [], 0.0, day_start(start)
    for donor in random.sample(person.strangers, min(random.randint(20, 60), len(person.strangers))):
        when = random_daytime(start + random.randint(0, random.randint(1, 3)))
        amount = random.choice([300, 500, 1000, 1000, 2000, 3000, 5000, 10_000])
        ops.append(tx(when, donor, person.card, amount, "transfer"))
        total, last = total + amount, max(last, when)
    ops.append(tx(after(last, 120, 20 * 60), person.card, CLINIC, round_to(total * random.uniform(0.85, 1.0), 1000),
                  "payment"))
    return ops


def joint_purchase(person: Person) -> list:
    """Организатор совместной покупки: участники платят за 1-2 дня, вся сумма уходит поставщику (частник)."""
    supplier = new_card()
    day = random.randint(5, DAYS - 4)
    count = random.randint(10, 25)
    known = random.sample(person.friends, min(len(person.friends), count * 3 // 10))
    participants = known + random.sample(person.strangers, count - len(known))
    ops, total, last = [op for friend in known for op in history(friend, person.card, 1, day)], 0.0, day_start(day)
    for participant in participants:
        when = random_daytime(day + random.randint(0, 1))
        amount = round_to(random.uniform(1500, 8000), 100)
        ops.append(tx(when, participant, person.card, amount, "transfer"))
        total, last = total + amount, max(last, when)
    ops.append(tx(after(last, 120, 20 * 60), person.card, supplier, round_to(total * random.uniform(0.95, 1.0), 100),
                  "transfer"))
    return ops


def relocation(person: Person) -> list:
    """Перед переездом распродаёт вещи за 2-3 дня, потом снимает почти всё наличными."""
    start = random.randint(5, DAYS - 5)
    ops, total, last = [], 0.0, day_start(start)
    for buyer in random.sample(person.strangers, random.randint(10, 20)):
        when = random_daytime(start + random.randint(0, 2))
        amount = round_to(random.uniform(2000, 40_000), 500)
        ops.append(tx(when, buyer, person.card, amount, "transfer"))
        total, last = total + amount, max(last, when)
    return ops + cash_out(person.card, total * random.uniform(0.8, 1.0), after(last, 120, 20 * 60), 120)


def job_loss(person: Person) -> list:
    """Потерял работу (пришёл только аванс), друзья помогают, деньги уходят на аренду."""
    landlord = new_card()
    day = random.randint(15, DAYS - 8)
    helpers = random.sample(person.friends, min(len(person.friends), random.randint(3, 6)))
    ops = [op for friend in helpers for op in history(person.card, friend, 1, day)]
    ops += [tx(random_daytime(day + random.randint(0, 6)), friend, person.card, random.randint(5, 20) * 1000,
               "transfer") for friend in helpers]
    ops.append(tx(random_daytime(day + 7), person.card, landlord, random.randint(25, 40) * 1000, "transfer"))
    return ops


SCENARIOS = {
    "mortgage": mortgage, "salary_cash": salary_cash, "pension_cash": pension_cash,
    "deposit_move": deposit_move, "car_sale": car_sale, "renovation": renovation,
    "avito": avito, "reseller": reseller, "taxi": taxi, "wedding": wedding, "fundraising": fundraising,
    "joint_purchase": joint_purchase, "relocation": relocation, "job_loss": job_loss,
}


def honest_people(clients: list) -> tuple[list, list]:
    """Честные люди во всех ситуациях по очереди. Возвращает (операции, разметка карта -> ситуация).

    Людей - пятая часть от числа клиентов, но не меньше, чем ситуаций. Работодателей
    несколько, у каждого по несколько сотрудников, как в жизни.
    """
    count = max(len(SITUATIONS), len(clients) // 5)
    employers = [f"Работодатель {i}" for i in range(1, max(2, count // 6) + 1)]
    situations = list(SITUATIONS)
    ops, labels = [], []
    for i in range(count):
        situation = situations[i % len(situations)]
        card = new_card()
        friends = random.sample(clients, random.randint(4, 8))
        person = Person(card, friends, [c for c in clients if c not in friends])
        if situation == "pension_cash":
            pension = True
        elif situation in ("salary_cash", "job_loss"):
            pension = False
        else:
            pension = random.random() < 0.25
        payer = PENSION_FUND if pension else random.choice(employers)
        ops += base_life(person, payer, pension, salary_parts=1 if situation == "job_loss" else 2,
                         min_salary=60 if situation == "salary_cash" else 45)
        ops += SCENARIOS[situation](person)
        labels.append({"card": card, "situation": situation})
    return ops, labels
