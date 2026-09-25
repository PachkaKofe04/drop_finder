"""
Поиск дроп-карт по трём схемам: транзит, воронка, быстрое обналичивание.

На вход - таблица в формате loader.py, то есть синтетика или реальная выгрузка.
Каждое срабатывание сопровождается причиной на человеческом языке, чтобы
аналитик видел, почему карта попала в список.

Запуск:
    python detectors.py data/transactions.csv
    python detectors.py data/transactions.csv --truth data/ground_truth.csv
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

from loader import ATM, EXTERNAL, load_ground_truth, load_transactions

DATA_DIR = Path(__file__).parent / "data"

TRANSIT, FUNNEL, FAST_CASHOUT = "transit", "funnel", "fast_cashout"
RULE_NAMES = [TRANSIT, FUNNEL, FAST_CASHOUT]

# Оценка риска - простая эвристика для сортировки списка, а не вероятность
SCORES = {TRANSIT: 70, FUNNEL: 85, FAST_CASHOUT: 75}
CHAIN_BONUS = 20       # транзитная карта связана с другими транзитными картами
EXTRA_RULE_BONUS = 5   # за каждое дополнительное сработавшее правило

EVENT_COLUMNS = ["card", "rule", "score", "time", "amount", "reason"]
SUMMARY_COLUMNS = ["card", "risk", "rules", "reason", "first_seen", "events"]


@dataclass(frozen=True)
class Rules:
    """Пороги правил.

    Намеренно мягче параметров generator.py, чтобы не подгонять детектор
    под собственную синтетику.
    """

    # Транзит: пришёл перевод, и почти та же сумма быстро ушла дальше
    transit_min_amount: float = 10_000
    transit_window: pd.Timedelta = pd.Timedelta(hours=6)
    transit_min_share: float = 0.90   # ушло не меньше 90% пришедшего...
    transit_max_share: float = 1.00   # ...и не больше, чем пришло (иначе это свои деньги)

    # Воронка: много разных отправителей за короткое время, затем деньги уходят
    funnel_window: pd.Timedelta = pd.Timedelta(hours=48)
    funnel_min_senders: int = 8
    funnel_min_total: float = 50_000
    funnel_outflow_window: pd.Timedelta = pd.Timedelta(hours=24)
    funnel_min_outflow_share: float = 0.80

    # Быстрое обналичивание: крупное поступление почти целиком снято наличными
    cashout_min_amount: float = 30_000
    cashout_window: pd.Timedelta = pd.Timedelta(hours=1)
    cashout_min_share: float = 0.80


# --- Быстрый поиск операций карты в интервале времени ------------------------

def _keys(codes, times) -> np.ndarray:
    """Число-ключ (карта, время): старшие биты - номер карты, младшие - секунды.

    Внутри одной карты ключи растут вместе со временем, поэтому все операции
    карты за интервал лежат подряд и находятся двоичным поиском.
    """
    seconds = np.asarray(times, dtype="datetime64[s]").astype(np.int64)
    return (np.asarray(codes, dtype=np.int64) << 32) + seconds


class CardFlows:
    """Операции карт, упорядоченные по карте и времени, с накопленной суммой.

    Отвечает на вопросы вида «сколько карта X потратила с момента A до момента B»
    сразу для тысяч пар (карта, интервал), без циклов по строкам.
    Карты задаются номерами (см. find_events), а не строками: так в разы быстрее.
    """

    def __init__(self, codes, times, amounts, counterparties):
        keys = _keys(codes, times)
        order = np.argsort(keys, kind="stable")
        self.keys = keys[order]
        self.codes = np.asarray(codes)[order]
        self.times = np.asarray(times, dtype="datetime64[s]")[order]
        self.counterparties = np.asarray(counterparties)[order]
        # cumsum[i] - сумма первых i операций, сумма операций [lo, hi) = cumsum[hi] - cumsum[lo]
        self.cumsum = np.concatenate([[0.0], np.cumsum(np.asarray(amounts, dtype=float)[order])])

    def bounds(self, codes, start, end) -> tuple[np.ndarray, np.ndarray]:
        """Номера операций [lo, hi) каждой карты внутри её интервала [start, end]."""
        lo = np.searchsorted(self.keys, _keys(codes, start), side="left")
        hi = np.searchsorted(self.keys, _keys(codes, end), side="right")
        return lo, hi

    def total(self, codes, start, end) -> np.ndarray:
        """Сумма операций каждой карты внутри её интервала [start, end]."""
        lo, hi = self.bounds(codes, start, end)
        return self.cumsum[hi] - self.cumsum[lo]


# --- Правила -------------------------------------------------------------------
# Функции правил получают операции с номерами карт (колонки sender_code и
# receiver_code), их добавляет find_events.

def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def _duration(delta) -> str:
    minutes = int(pd.Timedelta(delta).total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def _events(cards, rule, scores, times, amounts, reasons) -> pd.DataFrame:
    return pd.DataFrame({"card": cards, "rule": rule, "score": scores, "time": times,
                         "amount": amounts, "reason": reasons}, columns=EVENT_COLUMNS)


def find_transit(tx: pd.DataFrame, rules: Rules, out_flows: CardFlows) -> pd.DataFrame:
    """Транзит: на карту пришёл перевод, и почти та же сумма быстро ушла дальше.

    Дальше - это перевод, снятие или любая другая исходящая операция.
    Карты, которые передают деньги друг другу, склеиваются в цепочки.
    """
    incoming = tx[(tx["op_type"] == "transfer") & (tx["amount"] >= rules.transit_min_amount)]
    received = incoming["amount"].to_numpy(dtype=float)
    start = incoming["datetime"].to_numpy()
    lo, hi = out_flows.bounds(incoming["receiver_code"], start, start + rules.transit_window.to_timedelta64())

    # Ищем первую исходящую операцию, на которой ушедшая сумма достигла 90% пришедшей.
    # Накопленная сумма только растёт, поэтому хватает двоичного поиска.
    need = out_flows.cumsum[lo] + rules.transit_min_share * received
    k = np.searchsorted(out_flows.cumsum, need, side="left")   # операции [lo, k) дают нужную сумму
    in_window = k <= hi   # проверяем до обрезки k, иначе конец массива сойдёт за «нашли»
    k = np.minimum(k, len(out_flows.cumsum) - 1)
    sent = out_flows.cumsum[k] - out_flows.cumsum[lo]
    found = in_window & (sent <= rules.transit_max_share * received + 0.01) & (sent > 0)

    hits = incoming[found]
    last = k[found] - 1
    hits = hits.assign(sent=sent[found], out_time=out_flows.times[last], target=out_flows.counterparties[last])

    # Цепочки: граф «транзитная карта -> куда ушли деньги», связные компоненты из транзитных карт
    graph = nx.DiGraph()
    graph.add_nodes_from(hits["receiver_card"])
    graph.add_edges_from((a, b) for a, b in zip(hits["receiver_card"], hits["target"], strict=True)
                         if b in graph)
    chain_size = {card: len(part) for part in nx.weakly_connected_components(graph) for card in part}

    reasons, scores = [], []
    for row in hits.itertuples():
        where = "в банкомат" if row.target == ATM else f"на {row.target}"
        text = (f"Транзит: пришло {_money(row.amount)} от {row.sender_card}, "
                f"за {_duration(row.out_time - row.datetime)} ушло {_money(row.sent)} "
                f"({row.sent / row.amount:.0%}) {where}")
        size = chain_size[row.receiver_card]
        if size > 1:
            text += f", цепочка из {size} транзитных карт"
        reasons.append(text)
        scores.append(SCORES[TRANSIT] + (CHAIN_BONUS if size > 1 else 0))

    return _events(hits["receiver_card"].to_numpy(), TRANSIT, scores, hits["datetime"].to_numpy(),
                   hits["amount"].to_numpy(), reasons)


def find_funnels(tx: pd.DataFrame, rules: Rules, names: np.ndarray, out_flows: CardFlows) -> pd.DataFrame:
    """Воронка: за короткое время карта получает деньги от многих разных карт,
    а затем почти всё собранное уходит дальше.

    Второе условие отсекает легальные сборы денег (подарок, складчина):
    там деньги остаются на карте.
    """
    incoming = tx[tx["op_type"] == "transfer"]
    in_flows = CardFlows(incoming["receiver_code"], incoming["datetime"], incoming["amount"], incoming["sender_code"])

    # Каждое поступление - начало окна. Сначала дешёвые проверки для всех окон сразу:
    # число поступлений, собранная сумма и сколько ушло после последнего поступления.
    lo, hi = in_flows.bounds(in_flows.codes, in_flows.times, in_flows.times + rules.funnel_window.to_timedelta64())
    collected = in_flows.cumsum[hi] - in_flows.cumsum[lo]
    last_in = in_flows.times[np.maximum(hi - 1, 0)]
    sent = out_flows.total(in_flows.codes, last_in + np.timedelta64(1, "s"),
                           last_in + rules.funnel_outflow_window.to_timedelta64())
    candidates = np.flatnonzero((hi - lo >= rules.funnel_min_senders)
                                & (collected >= rules.funnel_min_total)
                                & (sent >= rules.funnel_min_outflow_share * collected))

    # Дорогая проверка - число разных отправителей - только для прошедших отбор окон.
    # Для каждой карты берём первое подходящее окно.
    cards, times, amounts, reasons = [], [], [], []
    for i in candidates:
        card = names[in_flows.codes[i]]
        senders = len(set(in_flows.counterparties[lo[i]:hi[i]]))
        if card in cards or senders < rules.funnel_min_senders:
            continue
        cards.append(card)
        times.append(in_flows.times[i])
        amounts.append(collected[i])
        reasons.append(f"Воронка: за {_duration(last_in[i] - in_flows.times[i])} пришло "
                       f"{_money(collected[i])} от {senders} разных карт, затем ушло "
                       f"{_money(sent[i])} ({sent[i] / collected[i]:.0%})")

    return _events(cards, FUNNEL, SCORES[FUNNEL], times, amounts, reasons)


def find_fast_cashouts(tx: pd.DataFrame, rules: Rules, cash_flows: CardFlows) -> pd.DataFrame:
    """Быстрое обналичивание: крупное поступление почти целиком снято наличными за короткое время."""
    incoming = tx[tx["op_type"].isin(["transfer", "top_up"]) & (tx["amount"] >= rules.cashout_min_amount)]
    received = incoming["amount"].to_numpy(dtype=float)
    start = incoming["datetime"].to_numpy()
    lo, hi = cash_flows.bounds(incoming["receiver_code"], start, start + rules.cashout_window.to_timedelta64())
    withdrawn = cash_flows.cumsum[hi] - cash_flows.cumsum[lo]
    found = (withdrawn >= rules.cashout_min_share * received) & (hi > lo)

    hits = incoming[found].assign(withdrawn=withdrawn[found], last_cash=cash_flows.times[hi[found] - 1])
    reasons = []
    for row in hits.itertuples():
        source = "пополнение" if row.sender_card == EXTERNAL else f"от {row.sender_card}"
        reasons.append(f"Быстрое обналичивание: пришло {_money(row.amount)} ({source}), "
                       f"за {_duration(row.last_cash - row.datetime)} снято {_money(row.withdrawn)} "
                       f"({row.withdrawn / row.amount:.0%})")

    return _events(hits["receiver_card"].to_numpy(), FAST_CASHOUT, SCORES[FAST_CASHOUT],
                   hits["datetime"].to_numpy(), hits["amount"].to_numpy(), reasons)


# --- Итог ------------------------------------------------------------------------

def find_events(tx: pd.DataFrame, rules: Rules | None = None) -> pd.DataFrame:
    """Все срабатывания всех правил: одна строка - одно срабатывание."""
    rules = rules or Rules()
    # Нумеруем карты один раз: дальше все поиски идут по числам, а не по строкам
    codes, names = pd.factorize(pd.concat([tx["sender_card"], tx["receiver_card"]], ignore_index=True))
    tx = tx.assign(sender_code=codes[:len(tx)], receiver_code=codes[len(tx):],
                   datetime=pd.to_datetime(tx["datetime"]))

    # Исходящие - всё, что карта отправила: переводы, снятия, прочие платежи
    out = tx[tx["sender_card"] != EXTERNAL]
    out_flows = CardFlows(out["sender_code"], out["datetime"], out["amount"], out["receiver_card"])
    cash = tx[tx["op_type"] == "cash_withdrawal"]
    cash_flows = CardFlows(cash["sender_code"], cash["datetime"], cash["amount"], cash["receiver_card"])

    parts = [
        find_transit(tx, rules, out_flows),
        find_funnels(tx, rules, np.asarray(names), out_flows),
        find_fast_cashouts(tx, rules, cash_flows),
    ]
    parts = [part for part in parts if not part.empty]
    if not parts:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    return pd.concat(parts, ignore_index=True)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    """Одна строка на карту: итоговый риск, сработавшие правила и главная причина."""
    if events.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)

    # Главная причина - срабатывание с наибольшей оценкой, при равенстве - самое раннее
    events = events.sort_values(["score", "time"], ascending=[False, True], kind="stable")
    summary = events.groupby("card", sort=False).agg(
        risk=("score", "max"),
        rules=("rule", lambda r: ", ".join(name for name in RULE_NAMES if name in set(r))),
        n_rules=("rule", "nunique"),
        reason=("reason", "first"),
        first_seen=("time", "min"),
        events=("rule", "size"),
    )
    summary["risk"] = np.minimum(100, summary["risk"] + EXTRA_RULE_BONUS * (summary["n_rules"] - 1))
    summary = summary.reset_index().sort_values(["risk", "first_seen"], ascending=[False, True], kind="stable")
    return summary[SUMMARY_COLUMNS].reset_index(drop=True)


def detect(tx: pd.DataFrame, rules: Rules | None = None) -> pd.DataFrame:
    """Подозрительные карты, от самых рискованных. Пустая таблица, если ничего не найдено."""
    return summarize(find_events(tx, rules))


def main():
    # Импорт здесь, чтобы metrics.py мог импортировать этот модуль без цикла
    from metrics import evaluate, format_report

    parser = argparse.ArgumentParser(description="Поиск дроп-карт по транзакциям")
    parser.add_argument("transactions", help="CSV с операциями: синтетика или реальная выгрузка")
    parser.add_argument("--truth", help="CSV с разметкой дроп-карт, чтобы посчитать точность")
    parser.add_argument("--out", default=str(DATA_DIR / "suspicious.csv"),
                        help="куда сохранить список подозрительных карт (по умолчанию data/suspicious.csv)")
    args = parser.parse_args()

    tx, warnings = load_transactions(args.transactions)
    for line in warnings:
        print("Внимание:", line)

    suspicious = detect(tx)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    suspicious.to_csv(args.out, index=False)

    n_cards = len((set(tx["sender_card"]) | set(tx["receiver_card"])) - {ATM, EXTERNAL})
    print(f"Подозрительных карт: {len(suspicious)} из {n_cards}. Полный список: {args.out}\n")
    with pd.option_context("display.max_colwidth", 200, "display.width", 250):
        print(suspicious.head(20)[["card", "risk", "rules", "reason"]].to_string(index=False))

    if args.truth:
        print()
        print(format_report(evaluate(suspicious, load_ground_truth(args.truth))))


if __name__ == "__main__":
    main()
