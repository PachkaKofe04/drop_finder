"""
Отслеживание денег: куда ушли деньги из одной операции.

Берём операцию (например, перевод жертвы на дроп-карту) и идём дальше по исходящим
операциям получателя в порядке времени. Допущение, как в расследованиях: первыми
с карты уходят именно прослеживаемые деньги, но не больше, чем пришло. Так шаг за
шагом видно, через какие карты прошли деньги, сколько из них снято наличными и
сколько осталось на картах.
"""

import heapq
import itertools
from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

from loader import ATM, EXTERNAL

CASHED, SETTLED = "Снято наличными", "Осталось на картах"
DEFAULT_HORIZON = pd.Timedelta(hours=72)
FLOW_COLUMNS = ["step", "time", "sender", "receiver", "amount", "op_type"]

# Цвета узлов Sankey (те же, что на графе связей)
COLOR_SOURCE = "#7fa7d4"
COLOR_SUSPICIOUS = "#e0413c"
COLOR_CARD = "#b8c4d6"
COLOR_CASHED = "#2e9e5b"
COLOR_SETTLED = "#a0a0a0"


@dataclass
class Trace:
    start: pd.Series         # операция, с которой начали
    flows: pd.DataFrame      # прослеженные части операций: шаг, время, откуда, куда, сколько, тип
    settled: pd.DataFrame    # где деньги остались: карта, сумма
    cashed: float            # сколько снято наличными

    @property
    def amount(self) -> float:
        return float(self.start["amount"])

    @property
    def settled_total(self) -> float:
        return float(self.settled["amount"].sum())

    @property
    def cards(self) -> int:
        """Через сколько карт прошли деньги (банкомат не считается)."""
        return self.flows.loc[self.flows["receiver"] != ATM, "receiver"].nunique()

    @property
    def duration(self) -> pd.Timedelta:
        """Сколько времени прошло от первой операции до последней прослеженной."""
        return self.flows["time"].max() - self.start["datetime"]

    @property
    def time_to_cash(self) -> pd.Timedelta | None:
        """Через сколько после первой операции деньги начали снимать наличными."""
        cash = self.flows.loc[self.flows["receiver"] == ATM, "time"]
        return cash.min() - self.start["datetime"] if len(cash) else None


def trace(tx: pd.DataFrame, start, horizon: pd.Timedelta = DEFAULT_HORIZON,
          max_steps: int = 6, min_amount: float = 1.0) -> Trace:
    """Прослеживает деньги операции tx.loc[start].

    horizon    - сколько времени после поступления смотреть исходящие операции карты
    max_steps  - сколько операций подряд прослеживать (первая тоже считается)
    min_amount - остаток меньше этой суммы дальше не прослеживаем
    """
    first = tx.loc[start]
    outgoing = tx[tx["sender_card"] != EXTERNAL].sort_values("datetime", kind="stable")
    by_card = outgoing.groupby("sender_card", sort=False).indices   # карта -> её операции по времени
    # Сколько денег уже прослежено через операцию: одни и те же деньги не должны
    # уйти одной операцией дважды, даже если вернулись на карту по кругу
    used = {}

    flows = [(1, first["datetime"], first["sender_card"], first["receiver_card"],
              float(first["amount"]), first["op_type"])]
    settled, cashed = {}, 0.0
    order = itertools.count()
    # Порции денег разбираем в порядке времени их прихода на карту
    queue = []
    if first["receiver_card"] == ATM:
        cashed = float(first["amount"])
    else:
        queue.append((first["datetime"], next(order), first["receiver_card"], float(first["amount"]), 1))

    while queue:
        arrived, _, card, amount, step = heapq.heappop(queue)
        if step < max_steps:
            card_ops = outgoing.iloc[by_card.get(card, [])]
            card_ops = card_ops[(card_ops["datetime"] > arrived) & (card_ops["datetime"] <= arrived + horizon)]
            for idx, op in card_ops.iterrows():
                if amount < min_amount:
                    break
                taken = min(amount, op["amount"] - used.get(idx, 0.0))
                if taken <= 0:
                    continue
                used[idx] = used.get(idx, 0.0) + taken
                amount -= taken
                flows.append((step + 1, op["datetime"], card, op["receiver_card"], taken, op["op_type"]))
                if op["receiver_card"] == ATM:
                    cashed += taken
                else:
                    heapq.heappush(queue, (op["datetime"], next(order), op["receiver_card"], taken, step + 1))
        if amount > 0:
            settled[card] = settled.get(card, 0.0) + amount

    settled_frame = (pd.DataFrame({"card": list(settled), "amount": list(settled.values())})
                     .sort_values("amount", ascending=False, ignore_index=True))
    return Trace(start=first, flows=pd.DataFrame(flows, columns=FLOW_COLUMNS),
                 settled=settled_frame, cashed=cashed)


def to_sankey(result: Trace, suspicious=frozenset()) -> go.Figure:
    """Диаграмма потоков: ширина полосы - сумма. Все деньги заканчиваются
    в одном из двух узлов: «Снято наличными» или «Осталось на картах»."""
    source_name = result.start["sender_card"]
    if source_name == EXTERNAL:
        source_name = "Пополнение"

    links = result.flows.assign(
        sender=result.flows["sender"].replace(EXTERNAL, "Пополнение"),
        receiver=result.flows["receiver"].replace(ATM, CASHED),
    )[["sender", "receiver", "amount"]]
    links = pd.concat([links, result.settled.rename(columns={"card": "sender"}).assign(receiver=SETTLED)])
    links = links.groupby(["sender", "receiver"], as_index=False, sort=False)["amount"].sum()

    names = list(dict.fromkeys([*links["sender"], *links["receiver"]]))
    position = {name: i for i, name in enumerate(names)}

    def color(name):
        if name == source_name:
            return COLOR_SOURCE
        return {CASHED: COLOR_CASHED, SETTLED: COLOR_SETTLED}.get(
            name, COLOR_SUSPICIOUS if name in suspicious else COLOR_CARD)

    def translucent(hex_color):
        """Цвет полосы - цвет узла, куда идут деньги, полупрозрачный."""
        red, green, blue = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
        return f"rgba({red}, {green}, {blue}, 0.35)"

    figure = go.Figure(go.Sankey(
        valueformat=",.0f",
        valuesuffix=" ₽",
        textfont={"size": 14, "shadow": "none"},   # без белой обводки подписи читаются лучше
        node={"label": names, "color": [color(n) for n in names], "pad": 24, "thickness": 18},
        link={"source": links["sender"].map(position), "target": links["receiver"].map(position),
              "value": links["amount"], "color": [translucent(color(n)) for n in links["receiver"]]},
    ))
    figure.update_layout(height=max(320, 70 * len(names)), margin={"l": 10, "r": 10, "t": 10, "b": 10})
    return figure
