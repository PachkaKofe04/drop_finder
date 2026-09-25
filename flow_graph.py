"""
Граф движения денег для дашборда.

networkx хранит структуру (кто кому сколько перевёл), pyvis рисует
интерактивную картинку. Банкомат и пополнение рисуются отдельным узлом
для каждой карты: иначе все снятия сошлись бы в одну точку, и граф
превратился бы в «ежа».
"""

import json

import networkx as nx
import pandas as pd
from pyvis.network import Network

from loader import ATM, EXTERNAL

# Цвета узлов (легенда - в app.py)
COLOR_SUSPICIOUS = "#e0413c"   # отмечена детектором
COLOR_MISSED = "#8e44ad"       # дроп по разметке, который детектор пропустил
COLOR_REGULAR = "#7fa7d4"      # обычная карта
COLOR_ATM = "#2e9e5b"
COLOR_EXTERNAL = "#a0a0a0"

# Настройки vis-network: сначала раскладываем граф и вписываем его в окно,
# толщина связи зависит от суммы, но в разумных пределах
VIS_OPTIONS = {
    "physics": {
        "solver": "barnesHut",
        "barnesHut": {"gravitationalConstant": -4000, "springLength": 140, "springConstant": 0.04,
                      "damping": 0.3, "avoidOverlap": 0.2},
        "stabilization": {"enabled": True, "iterations": 400, "fit": True},
    },
    "edges": {"smooth": {"type": "continuous"}, "scaling": {"min": 1, "max": 6},
              "arrows": {"to": {"enabled": True, "scaleFactor": 0.6}}},
    "nodes": {"font": {"size": 13}},
    "interaction": {"hover": True, "tooltipDelay": 100},
}

# После раскладки физику выключаем, иначе несвязанные группы узлов продолжают
# разъезжаться за край окна. Кроме того, граф может создаваться во вкладке, которая
# ещё скрыта, и тогда vis-network вписывает его в окно нулевого размера: поэтому
# вписываем заново, когда окно получает настоящий размер.
REFIT_SCRIPT = """
<script>
  network.once("stabilizationIterationsDone", () => {
    network.setOptions({physics: false});
    network.fit();
  });
  new ResizeObserver(() => { network.redraw(); network.fit(); })
    .observe(document.getElementById("mynetwork"));
</script>
"""


def build_graph(tx: pd.DataFrame, centers, suspicious: dict, drops=frozenset(),
                depth: int = 1, max_edges: int = 150) -> nx.DiGraph:
    """Операции вокруг карт centers.

    Берём все прямые связи центральных карт, а дальше идём только через
    подозрительные карты (не больше depth шагов). Так видна цепочка, по которой
    шли деньги, и граф не тонет в друзьях друзей.

    suspicious - словарь «карта -> риск», drops - карты-дропы по разметке (если есть).
    """
    centers = set(centers)
    seen = set(centers)
    frontier = set(centers)
    selected = pd.Series(False, index=tx.index)
    for _ in range(depth):
        if not frontier:
            break
        step = tx["sender_card"].isin(frontier) | tx["receiver_card"].isin(frontier)
        selected |= step
        neighbours = set(tx.loc[step, "sender_card"]) | set(tx.loc[step, "receiver_card"])
        frontier = {card for card in neighbours - seen if card in suspicious}
        seen |= neighbours

    ops = tx[selected]
    # Отдельный банкомат и отдельное «пополнение» у каждой карты
    source = ops["sender_card"].where(ops["sender_card"] != EXTERNAL, EXTERNAL + ":" + ops["receiver_card"])
    target = ops["receiver_card"].where(ops["receiver_card"] != ATM, ATM + ":" + ops["sender_card"])
    edges = (ops.assign(source=source, target=target)
                .groupby(["source", "target"], as_index=False)
                .agg(amount=("amount", "sum"), count=("amount", "size"),
                     first=("datetime", "min"), last=("datetime", "max")))

    # Если связей слишком много, оставляем связи центральных карт и самые крупные из остальных
    touches_center = edges["source"].isin(centers) | edges["target"].isin(centers)
    edges = (edges.assign(touches_center=touches_center)
                  .sort_values(["touches_center", "amount"], ascending=False)
                  .head(max_edges))

    graph = nx.DiGraph()
    for row in edges.itertuples():
        for node in (row.source, row.target):
            if node not in graph:
                graph.add_node(node, **_node_style(node, centers, suspicious, drops))
        period = f"{row.first:%d.%m %H:%M}" + ("" if row.first == row.last else f" - {row.last:%d.%m %H:%M}")
        graph.add_edge(row.source, row.target, amount=row.amount,
                       title=f"{row.count} опер., {_money(row.amount)}\n{period}")
    return graph


def _node_style(node: str, centers: set, suspicious: dict, drops) -> dict:
    """Подпись, цвет, форма и подсказка узла."""
    if node.startswith(ATM + ":"):
        return {"label": "Банкомат", "title": "Снятие наличных", "color": COLOR_ATM, "shape": "box", "size": 12}
    if node.startswith(EXTERNAL + ":"):
        return {"label": "Пополнение", "title": "Деньги извне: зарплата, другой банк, наличные",
                "color": COLOR_EXTERNAL, "shape": "box", "size": 12}

    lines = [node]
    if node in suspicious:
        color, size = COLOR_SUSPICIOUS, 18 + suspicious[node] / 10
        lines.append(f"Риск {suspicious[node]}")
    elif node in drops:
        color, size = COLOR_MISSED, 18
        lines.append("Дроп по разметке, детектор пропустил")
    else:
        color, size = COLOR_REGULAR, 12
    if drops and node in suspicious:
        lines.append("Дроп по разметке" if node in drops else "По разметке не дроп")
    if node in centers:
        size += 8
    return {"label": node, "title": "\n".join(lines), "color": color, "shape": "dot", "size": size}


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def to_html(graph: nx.DiGraph, height: int = 600) -> str:
    """Интерактивная картинка графа (HTML со встроенным vis-network).

    Номера карт могут прийти из загруженного файла, но экранировать их не нужно:
    pyvis передаёт узлы и связи через фильтр tojson, он кодирует <, > и &,
    а vis-network показывает подсказки как текст, а не как HTML.
    """
    net = Network(height=f"{height}px", width="100%", directed=True, cdn_resources="remote")
    net.set_options(json.dumps(VIS_OPTIONS))
    for node, data in graph.nodes(data=True):
        net.add_node(node, label=data["label"], title=data["title"],
                     color=data["color"], shape=data["shape"], size=data["size"])
    for source, target, data in graph.edges(data=True):
        net.add_edge(source, target, title=data["title"], value=data["amount"])
    return net.generate_html().replace("</body>", REFIT_SCRIPT + "</body>")
