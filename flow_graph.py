"""
Граф движения денег для дашборда.

networkx хранит структуру (кто кому сколько перевёл), vis-network рисует
интерактивную картинку. Деньги текут слева направо: откуда пришли, через какие
карты прошли, где сняты. Банкомат и пополнение рисуются отдельным узлом для
каждой карты: иначе все снятия сошлись бы в одну точку, и граф превратился бы в «ежа».
"""

import json
from collections import defaultdict
from functools import partial
from string import Template

import networkx as nx
import pandas as pd

import theme
from loader import ATM, EXTERNAL

# Виды узлов: цвет и подпись в легенде
SUSPICIOUS, MISSED, REGULAR, CASH, INCOME = "suspicious", "missed", "regular", "cash", "income"
KINDS = {
    SUSPICIOUS: (theme.RISK, "подозрительная"),
    MISSED: (theme.MISSED, "пропущенный дроп"),
    REGULAR: (theme.REGULAR, "обычная карта"),
    CASH: (theme.CASH, "снятие наличных"),
    INCOME: (theme.INCOME, "пополнение"),
}

# Раскладка: колонки слева направо, узлы колонки друг под другом, группы карт рядами.
# Общий граф плотнее и с крупным шрифтом: его всегда приходится уменьшать, чтобы
# он поместился в окно, а подписи должны оставаться читаемыми.
MODES = {
    True: {"layer_gap": 200, "row_gap": 64, "font": 14},    # разбор одной карты
    False: {"layer_gap": 150, "row_gap": 58, "font": 17},   # общий граф
}
GROUP_GAP = 110
MAX_SCALE = 1.3   # маленький граф не раздуваем на всё окно


def _options(font_size: int) -> dict:
    """Настройки vis-network. Физика выключена: координаты узлов считаются заранее
    (см. _layout), поэтому картинка не «плывёт» и сразу вписывается в окно.
    Толщина связи зависит от суммы, размер подписи на связи - нет."""
    return {
        "physics": {"enabled": False},
        "nodes": {
            "borderWidth": 2,
            "font": {"face": theme.FONT, "size": font_size, "color": theme.TEXT_2, "strokeWidth": 4,
                     "strokeColor": theme.BG, "vadjust": 2},
            "shadow": {"enabled": False, "size": 18, "x": 0, "y": 0},
        },
        "edges": {
            "smooth": {"type": "cubicBezier", "forceDirection": "horizontal", "roundness": 0.5},
            "scaling": {"min": 1, "max": 4, "label": {"enabled": False}},
            "arrows": {"to": {"enabled": True, "scaleFactor": 0.35}},
            "arrowStrikethrough": False,
            "hoverWidth": 0.6,
            "selectionWidth": 0.8,
            "font": {"face": theme.FONT, "size": font_size - 2, "color": theme.TEXT_3, "strokeWidth": 4,
                     "strokeColor": theme.BG, "align": "top"},
        },
        "interaction": {"hover": True, "tooltipDelay": 80},
    }

PAGE = Template("""<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="$font_url">
<link rel="stylesheet" crossorigin="anonymous" referrerpolicy="no-referrer"
  href="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/dist/vis-network.min.css"
  integrity="sha512-WgxfT5LWjfszlPHXRmBWHkV2eceiWTOBvrKCNbdgDYTHrT2AeLCGbF4sZlZw3UMN3WtL0tGUoIAKsu8mllg/XA==">
<script crossorigin="anonymous" referrerpolicy="no-referrer"
  src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.2/dist/vis-network.min.js"
  integrity="sha512-LnvoEWDFrqGHlHmDD2101OrLcbsfkrzoSpvtSQtxK3RMnRV0eOkhhBN2dXHKRrUU8p2DGRTk35n4O8nWSVe1mQ=="></script>
<style>
  html, body { margin: 0; background: $bg; }
  .frame {
    position: relative; height: ${height}px; box-sizing: border-box; overflow: hidden;
    border: 1px solid $hairline; border-radius: 18px;
    background: radial-gradient(ellipse at 30% 0%, rgba(143, 185, 223, 0.06), transparent 60%),
                linear-gradient(160deg, #111820, #0c1117 70%);
  }
  /* vis-network очищает свой контейнер, поэтому легенда лежит рядом с ним, а не внутри */
  #graph { position: absolute; inset: 38px 0 0 0; }
  .legend {
    position: absolute; top: 14px; left: 18px; display: flex; flex-wrap: wrap; gap: 6px 18px;
    font: 500 11px/16px $font; color: $text_2;
  }
  .legend i { display: inline-block; width: 8px; height: 8px; margin-right: 6px; border-radius: 50%; }
  .legend i.cash { border-radius: 2px; }
  .legend i.income { border-radius: 1px; transform: rotate(45deg) scale(0.85); }
  div.vis-tooltip {
    padding: 8px 11px; border: 1px solid $hairline_strong; border-radius: 10px;
    background: $surface_2; color: $text; font: 12px/1.5 $font; white-space: pre-line;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.45);
  }
</style></head>
<body>
<div class="frame"><div class="legend">$legend</div><div id="graph"></div></div>
<script>
  const nodes = new vis.DataSet($nodes);
  const edges = new vis.DataSet($edges);
  const container = document.getElementById("graph");
  const network = new vis.Network(container, {nodes, edges}, $options);
  // Граф может создаваться во вкладке, которая ещё скрыта: тогда vis-network вписывает
  // его в окно нулевого размера. Вписываем заново, когда окно получает настоящий размер.
  new ResizeObserver(() => {
    network.redraw();
    network.fit();
    if (network.getScale() > $max_scale) network.moveTo({scale: $max_scale});
  }).observe(container);
</script>
</body></html>
""")


def build_graph(tx: pd.DataFrame, centers, suspicious: dict, drops=frozenset(),
                depth: int = 1, max_edges: int = 150, group_from: int = 5) -> nx.DiGraph:
    """Операции вокруг карт centers.

    Берём все прямые связи центральных карт, а дальше идём только через
    подозрительные карты (не больше depth шагов). Так видна цепочка, по которой
    шли деньги, и граф не тонет в друзьях друзей.

    suspicious - словарь «карта -> риск», drops - карты-дропы по разметке (если есть).
    group_from - со скольких одинаково связанных обычных карт сворачивать их в один узел.
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
                graph.add_node(node, **_node_info(node, centers, suspicious, drops))
        _add_edge(graph, row.source, row.target, row.amount, row.count, row.first, row.last)
    _group_leaves(graph, group_from)
    return graph


def _add_edge(graph: nx.DiGraph, source, target, amount, count, first, last):
    period = f"{first:%d.%m %H:%M}" + ("" if first == last else f" - {last:%d.%m %H:%M}")
    graph.add_edge(source, target, amount=amount, count=count, first=first, last=last,
                   title=f"{count} опер., {_money(amount)}\n{period}")


def _cards(count: int) -> str:
    """'1 карта', '3 карты', '12 карт'."""
    if count % 10 == 1 and count % 100 != 11:
        return f"{count} карта"
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return f"{count} карты"
    return f"{count} карт"


def _group_leaves(graph: nx.DiGraph, min_count: int):
    """Обычные карты, связанные только с одной картой и в одну сторону (отправители
    в воронку, знакомые), сворачиваются в один узел «12 карт» со списком в подсказке.
    Иначе воронка превращается в столбик из десятков точек, а подписи - в кашу.
    """
    for anchor in list(graph):
        for incoming in (True, False):
            if anchor not in graph:   # сама оказалась листом и уже свёрнута
                break
            if incoming:
                leaves = [n for n in graph.predecessors(anchor) if graph.in_degree(n) == 0 and graph.out_degree(n) == 1]
            else:
                leaves = [n for n in graph.successors(anchor) if graph.out_degree(n) == 0 and graph.in_degree(n) == 1]
            leaves = sorted(n for n in leaves if graph.nodes[n]["kind"] == REGULAR and not graph.nodes[n]["center"])
            if len(leaves) < min_count:
                continue
            edges = [graph.edges[(leaf, anchor) if incoming else (anchor, leaf)] for leaf in leaves]
            group = f"group:{'in' if incoming else 'out'}:{anchor}"
            shown = leaves[:15] + ([f"и ещё {len(leaves) - 15}"] if len(leaves) > 15 else [])
            graph.add_node(group, kind=REGULAR, label=_cards(len(leaves)), size=8, center=False, group=True,
                           title=f"{'Отправители' if incoming else 'Получатели'}: {_cards(len(leaves))}\n"
                                 + "\n".join(shown))
            ends = (group, anchor) if incoming else (anchor, group)
            _add_edge(graph, *ends, sum(e["amount"] for e in edges), sum(e["count"] for e in edges),
                      min(e["first"] for e in edges), max(e["last"] for e in edges))
            graph.remove_nodes_from(leaves)


def _node_info(node: str, centers: set, suspicious: dict, drops) -> dict:
    """Вид узла, подпись, подсказка и размер."""
    if node.startswith(ATM + ":"):
        return {"kind": CASH, "label": "банкомат", "title": "Снятие наличных", "size": 6, "center": False}
    if node.startswith(EXTERNAL + ":"):
        return {"kind": INCOME, "label": "пополнение", "title": "Деньги извне: зарплата, другой банк, наличные",
                "size": 6, "center": False}

    lines = [node]
    if node in suspicious:
        kind, size = SUSPICIOUS, 9
        lines.append(f"Риск {suspicious[node]}")
    elif node in drops:
        kind, size = MISSED, 9
        lines.append("Дроп по разметке, детектор пропустил")
    else:
        kind, size = REGULAR, 6
    if drops and node in suspicious:
        lines.append("Дроп по разметке" if node in drops else "По разметке не дроп")
    center = node in centers
    return {"kind": kind, "label": theme.short_card(node), "title": "\n".join(lines),
            "size": size + 4 * center, "center": center}


# --- Раскладка ------------------------------------------------------------------

def _layers(graph: nx.DiGraph) -> dict:
    """Номер колонки каждого узла: деньги текут слева направо.

    Карты, переводящие друг другу по кругу, стоят в одной колонке. Источники
    (пополнение, жертва) ставятся вплотную к получателю, а не в самую левую колонку.
    """
    dag = nx.condensation(graph)
    level = {}
    for part in nx.topological_sort(dag):
        level[part] = max((level[p] + 1 for p in dag.predecessors(part)), default=0)
    for part in dag.nodes:
        if dag.in_degree(part) == 0 and dag.out_degree(part) > 0:
            level[part] = min(level[s] for s in dag.successors(part)) - 1
    mapping = dag.graph["mapping"]
    return {node: level[mapping[node]] for node in graph}


def _arrange(graph: nx.DiGraph, group, layer_gap: float, row_gap: float) -> tuple[dict, float, float]:
    """Координаты узлов одной связной группы, её ширина и высота.

    Порядок в колонке подбирается по средней высоте соседей (метод барицентров),
    чтобы связи меньше пересекались.
    """
    sub = graph.subgraph(group)
    layer = _layers(sub)
    first = min(layer.values())
    columns = defaultdict(list)
    for node in sorted(sub, key=lambda n: (-sub.degree(n, weight="amount"), n)):
        columns[layer[node] - first].append(node)

    def y_of(column):
        return {node: (i - (len(column) - 1) / 2) * row_gap for i, node in enumerate(column)}

    def barycenter(node, neighbours):
        ys = [y[n] for n in neighbours(node)]
        return sum(ys) / len(ys) if ys else y[node]

    y = {}
    for column in columns.values():
        y.update(y_of(column))
    order = sorted(columns)
    for sweep in range(4):   # проходы слева направо и обратно
        left_to_right = sweep % 2 == 0
        neighbours = sub.predecessors if left_to_right else sub.successors
        for index in order if left_to_right else reversed(order):
            columns[index].sort(key=partial(barycenter, neighbours=neighbours))
            y.update(y_of(columns[index]))

    positions = {node: (layer_index * layer_gap, y[node]) for layer_index, column in columns.items() for node in column}
    width = max(columns) * layer_gap
    height = (max(len(column) for column in columns.values()) - 1) * row_gap
    return positions, width, height


def _shelves(arranged: list, row_limit: float) -> tuple[dict, float, float]:
    """Группы рядами, как слова в строке: координаты, общая ширина и высота."""
    positions, x, y, row_height, width = {}, 0.0, 0.0, 0.0, 0.0
    for local, group_width, group_height in arranged:
        if x > 0 and x + group_width > row_limit:
            x, y, row_height = 0.0, y + row_height + GROUP_GAP, 0.0
        for node, (node_x, node_y) in local.items():
            positions[node] = (x + node_x, y + group_height / 2 + node_y)
        width = max(width, x + group_width)
        x += group_width + GROUP_GAP
        row_height = max(row_height, group_height)
    return positions, width, y + row_height


def _layout(graph: nx.DiGraph, layer_gap: float, row_gap: float, aspect: float = 16 / 9) -> dict:
    """Координаты узлов в пикселях.

    Считаем здесь, а не физикой в браузере: там раскладка асинхронная и иногда
    заканчивается уже после того, как граф вписали в окно. Картинка ещё и
    одинаковая при каждом открытии.

    Каждую связную группу карт раскладываем отдельно и ставим группы рядами,
    высокие первыми. Ширину ряда подбираем так, чтобы всё вместе вписалось
    в окно с пропорцией aspect как можно крупнее.
    """
    groups = sorted(nx.weakly_connected_components(graph), key=min)
    arranged = sorted((_arrange(graph, group, layer_gap, row_gap) for group in groups),
                      key=lambda a: (-a[2], -a[1]))
    if not arranged:
        return {}
    widest = max(width for _, width, _ in arranged)
    total = sum(width for _, width, _ in arranged) + GROUP_GAP * (len(arranged) - 1)
    best_scale, best = -1.0, {}
    for step in range(41):
        positions, width, height = _shelves(arranged, widest + (total - widest) * step / 40)
        scale = min(aspect / max(width, 1.0), 1 / max(height, 1.0))
        if scale > best_scale:
            best_scale, best = scale, positions
    return best


# --- Картинка ---------------------------------------------------------------------

def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def _compact(value: float) -> str:
    """Сумма для подписи на связи: '9 800 ₽', '173 тыс ₽', '1,2 млн ₽'."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f} млн ₽".replace(".", ",")
    if value >= 10_000:
        return f"{value / 1000:.0f} тыс ₽"
    return _money(value)


def _node_style(data: dict, detailed: bool) -> dict:
    """Цвет, форма и подпись узла для vis-network."""
    font_size = MODES[detailed]["font"]
    color = KINDS[data["kind"]][0]
    shown = detailed or data["kind"] in (SUSPICIOUS, MISSED) or data.get("group")
    style = {"label": data["label"] if shown else "",
             "title": data["title"], "size": data["size"], "shape": "dot"}
    if data["kind"] == SUSPICIOUS:
        # Заливка со светлым кольцом и мягким свечением: главное на картинке
        fill = {"background": color, "border": "#ffd5da"}
        style["shadow"] = {"enabled": True, "color": theme.rgba(color, 0.55)}
        style["font"] = {"color": theme.TEXT}
    elif data["kind"] in (CASH, INCOME):
        fill = {"background": color, "border": color}
        style["shape"] = "square" if data["kind"] == CASH else "diamond"
        style["font"] = {"size": font_size - 3, "color": theme.TEXT_3}
    else:
        # Обычная карта и пропущенный дроп - кольцо с тёмной серединой
        fill = {"background": theme.rgba(color, 0.25) if data["kind"] == MISSED else theme.SURFACE_2,
                "border": color}
    style["color"] = {**fill, "highlight": fill, "hover": fill}
    if data["center"]:
        style["font"] = {"color": theme.TEXT, "size": font_size + 1}
    return style


def _edge_color(source_kind: str, target_kind: str) -> str:
    """Связь окрашена по самому важному концу, остальные - нейтральные."""
    if target_kind == CASH:
        return theme.rgba(theme.CASH, 0.6)
    if SUSPICIOUS in (source_kind, target_kind):
        return theme.rgba(theme.RISK, 0.55)
    if MISSED in (source_kind, target_kind):
        return theme.rgba(theme.MISSED, 0.5)
    return theme.rgba(theme.TEXT_2, 0.28)


def _json(value) -> str:
    """JSON для вставки в <script>: <, > и & кодируются, чтобы данные из файла не закрыли тег."""
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def vis_data(graph: nx.DiGraph, detailed: bool = False) -> tuple[list, list]:
    """Узлы и связи в формате vis-network, с готовыми координатами."""
    mode = MODES[detailed]
    positions = _layout(graph, mode["layer_gap"], mode["row_gap"])
    nodes = [{"id": node, "x": round(positions[node][0], 1), "y": round(positions[node][1], 1),
              **_node_style(data, detailed)} for node, data in graph.nodes(data=True)]
    edges = []
    for source, target, data in graph.edges(data=True):
        color = _edge_color(graph.nodes[source]["kind"], graph.nodes[target]["kind"])
        edge = {"from": source, "to": target, "value": data["amount"], "title": data["title"],
                "color": {"color": color, "highlight": theme.ACCENT, "hover": theme.ACCENT}}
        if detailed:
            edge["label"] = _compact(data["amount"])
        edges.append(edge)
    return nodes, edges


def to_html(graph: nx.DiGraph, height: int = 600, detailed: bool = False) -> str:
    """Интерактивная картинка графа (HTML со встроенным vis-network).

    detailed - подписи у всех узлов и суммы на связях; для разбора одной карты.
    В общем графе подписаны только подозрительные карты, остальное - в подсказках.

    Номера карт могут прийти из загруженного файла: в скрипт они попадают через
    _json, а vis-network показывает подсказки как текст, а не как HTML.
    """
    nodes, edges = vis_data(graph, detailed)
    present = {data["kind"] for _, data in graph.nodes(data=True)}
    legend = "".join(f'<span><i class="{kind}" style="background: {color}"></i>{label}</span>'
                     for kind, (color, label) in KINDS.items() if kind in present)
    return PAGE.substitute(
        nodes=_json(nodes), edges=_json(edges), options=json.dumps(_options(MODES[detailed]["font"])), legend=legend,
        height=height, max_scale=MAX_SCALE, font_url=theme.FONT_URL, font=theme.FONT, bg=theme.BG,
        text=theme.TEXT, text_2=theme.TEXT_2, surface_2=theme.SURFACE_2,
        hairline=theme.HAIRLINE, hairline_strong=theme.HAIRLINE_STRONG,
    )
