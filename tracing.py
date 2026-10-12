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
from html import escape

import pandas as pd
import plotly.graph_objects as go

import theme
from loader import ATM, EXTERNAL

CASHED, SETTLED = "Снято наличными", "Осталось на картах"
DEFAULT_HORIZON = pd.Timedelta(hours=72)
FLOW_COLUMNS = ["step", "time", "sender", "receiver", "amount", "op_type"]


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
    в одном из двух узлов: «Снято наличными» или «Осталось на картах».

    Цвета те же, что на графе связей. Полосы окрашены, только когда идут
    к подозрительной карте или в наличные, остальные - нейтральные.
    """
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
    # Сколько денег прошло через узел: в источник ничего не приходит, для него - вся сумма
    through = links.groupby("receiver")["amount"].sum().to_dict() | {source_name: result.amount}

    def color(name):
        if name == source_name:
            return theme.INCOME
        return {CASHED: theme.CASH, SETTLED: theme.SETTLED}.get(
            name, theme.RISK if name in suspicious else theme.REGULAR)

    def link_color(target):
        if target == CASHED:
            return theme.rgba(theme.CASH, 0.22)
        return theme.rgba(theme.RISK, 0.13) if target in suspicious else theme.rgba(theme.TEXT_2, 0.1)

    def label(name):
        """Короткое имя и сумма; номера карт из файла экранируются, plotly понимает часть HTML."""
        short = name if name in (CASHED, SETTLED, "Пополнение") else theme.short_card(name)
        return f"{escape(short)}<br><span style='color:{theme.TEXT_2}'>{through[name]:,.0f} ₽</span>".replace(",", " ")

    figure = go.Figure(go.Sankey(
        valueformat=",.0f",
        valuesuffix=" ₽",
        textfont={"family": theme.FONT, "size": 12, "color": theme.TEXT, "shadow": "none"},
        node={"label": [label(n) for n in names], "color": [color(n) for n in names],
              "customdata": [escape(n) for n in names], "hovertemplate": "%{customdata}<extra></extra>",
              "pad": 34, "thickness": 10, "line": {"width": 0}},
        link={"source": links["sender"].map(position), "target": links["receiver"].map(position),
              "value": links["amount"], "color": [link_color(n) for n in links["receiver"]],
              "hovertemplate": "%{value} <extra></extra>"},
    ))
    figure.update_layout(
        height=max(340, 76 * len(names)), margin={"l": 8, "r": 8, "t": 16, "b": 30},
        separators=", ", font={"family": theme.FONT, "color": theme.TEXT},
        paper_bgcolor="rgba(0, 0, 0, 0)", plot_bgcolor="rgba(0, 0, 0, 0)",
        hoverlabel={"bgcolor": theme.SURFACE_2, "bordercolor": theme.HAIRLINE_STRONG,
                    "font": {"family": theme.FONT, "color": theme.TEXT, "size": 12}},
    )
    return figure
