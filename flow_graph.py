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

# Фон, подписи и рамка под светлую и тёмную тему Streamlit
THEMES = {
    "light": {"background": "#ffffff", "font": "#31333f", "border": "#e6e9ef"},
    "dark": {"background": "#0e1117", "font": "#fafafa", "border": "#31333f"},
}

# Настройки vis-network. Физика выключена: координаты узлов считаются заранее
# (см. _layout), поэтому картинка не «плывёт» и сразу вписывается в окно.
# Толщина связи зависит от суммы, но в разумных пределах.
VIS_OPTIONS = {
    "physics": {"enabled": False},
    "edges": {"smooth": {"type": "continuous"}, "scaling": {"min": 1, "max": 6},
              "arrows": {"to": {"enabled": True, "scaleFactor": 0.6}}},
    "nodes": {"font": {"size": 13}},
    "interaction": {"hover": True, "tooltipDelay": 100},
}

# Граф может создаваться во вкладке, которая ещё скрыта: тогда vis-network вписывает
# его в окно нулевого размера. Вписываем заново, когда окно получает настоящий размер.
REFIT_SCRIPT = """
<script>
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


def _layout(graph: nx.DiGraph) -> dict:
    """Координаты узлов в пикселях.

    Считаем здесь, а не физикой в браузере: там раскладка асинхронная и иногда
    заканчивается уже после того, как граф вписали в окно. С фиксированным seed
    картинка ещё и одинаковая при каждом открытии.

    Каждую связную группу карт раскладываем отдельно и ставим группы рядами,
    как слова в строке: общая раскладка разбросала бы группы по краям окна.
    """
    undirected = graph.to_undirected()
    groups = sorted(nx.connected_components(undirected), key=lambda g: (-len(g), min(g)))
    radius = {id(g): 80 * len(g) ** 0.5 for g in groups}
    gap = 60
    row_limit = 1.8 * sum((2 * r) ** 2 for r in radius.values()) ** 0.5   # окно примерно 16:9

    positions, x, y, row_height = {}, 0.0, 0.0, 0.0
    for group in groups:
        r = radius[id(group)]
        if x > 0 and x + 2 * r > row_limit:
            x, y, row_height = 0.0, y + row_height + gap, 0.0
        local = (nx.spring_layout(undirected.subgraph(group), k=3 / len(group) ** 0.5, seed=42, iterations=200, scale=r)
                 if len(group) > 1 else {next(iter(group)): (0.0, 0.0)})
        for node, (node_x, node_y) in local.items():
            positions[node] = (x + r + node_x, y + r + node_y)
        x += 2 * r + gap
        row_height = max(row_height, 2 * r)
    return positions


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def to_html(graph: nx.DiGraph, height: int = 600, theme: str = "light") -> str:
    """Интерактивная картинка графа (HTML со встроенным vis-network).

    Номера карт могут прийти из загруженного файла, но экранировать их не нужно:
    pyvis передаёт узлы и связи через фильтр tojson, он кодирует <, > и &,
    а vis-network показывает подсказки как текст, а не как HTML.
    """
    colors = THEMES.get(theme, THEMES["light"])
    net = Network(height=f"{height}px", width="100%", directed=True, cdn_resources="remote",
                  bgcolor=colors["background"], font_color=colors["font"])
    net.set_options(json.dumps(VIS_OPTIONS))
    positions = _layout(graph)
    for node, data in graph.nodes(data=True):
        x, y = positions[node]
        net.add_node(node, label=data["label"], title=data["title"], color=data["color"],
                     shape=data["shape"], size=data["size"], x=float(x), y=float(y))
    for source, target, data in graph.edges(data=True):
        net.add_edge(source, target, title=data["title"], value=data["amount"])
    # Шаблон pyvis оборачивает граф в белую карточку с серой рамкой - перекрашиваем под тему
    style = (f"<style>body {{ margin: 0; background: {colors['background']}; }} "
             f".card {{ background: transparent; border: none; }} "
             f"#mynetwork {{ border: 1px solid {colors['border']} !important; }}</style>")
    page = net.generate_html()
    return page.replace("</head>", style + "</head>").replace("</body>", REFIT_SCRIPT + "</body>")
