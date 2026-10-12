"""Тесты графа движения денег."""

import json
import re

import pandas as pd

import theme
from flow_graph import CASH, INCOME, MISSED, REGULAR, SUSPICIOUS, build_graph, to_html, vis_data
from loader import ATM, COLUMNS, EXTERNAL

T0 = pd.Timestamp("2026-08-10 10:00")


def ops(*rows) -> pd.DataFrame:
    """Операции из кортежей (минуты от T0, отправитель, получатель, сумма, тип)."""
    return pd.DataFrame([(T0 + pd.Timedelta(minutes=m), sender, receiver, float(amount), op_type)
                         for m, sender, receiver, amount, op_type in rows], columns=COLUMNS)


# Жертва переводит дропу 1, тот - дропу 2, тот снимает. У жертвы есть друг.
CHAIN = ops(
    (0, "friend", "victim", 1_000, "transfer"),
    (10, "victim", "drop1", 100_000, "transfer"),
    (100, "drop1", "drop2", 98_500, "transfer"),
    (200, "drop2", ATM, 97_000, "cash_withdrawal"),
    (300, EXTERNAL, "drop1", 3_000, "top_up"),
)
RISK = {"drop1": 90, "drop2": 90}


def page_data(page: str, name: str) -> list:
    """Узлы или связи, встроенные в HTML-страницу графа."""
    return json.loads(re.search(rf"const {name} = new vis.DataSet\((.*)\);", page).group(1))


def test_chain_is_followed_through_suspicious_cards():
    graph = build_graph(CHAIN, ["drop1"], RISK, depth=3)
    assert set(graph.nodes) == {"victim", "drop1", "drop2", f"{ATM}:drop2", f"{EXTERNAL}:drop1"}
    # Жертва не подозрительная, поэтому к её друзьям граф не идёт
    assert "friend" not in graph


def test_depth_limits_the_chain():
    graph = build_graph(CHAIN, ["drop1"], RISK, depth=1)
    assert "drop2" in graph
    assert f"{ATM}:drop2" not in graph


def test_atm_is_separate_for_each_card():
    tx = ops((0, "a", ATM, 1_000, "cash_withdrawal"), (5, "b", ATM, 2_000, "cash_withdrawal"))
    graph = build_graph(tx, ["a", "b"], {})
    assert {f"{ATM}:a", f"{ATM}:b"} <= set(graph.nodes)
    assert graph.nodes[f"{ATM}:a"]["label"] == "банкомат"
    assert graph.nodes[f"{ATM}:a"]["kind"] == CASH


def test_operations_between_two_cards_are_one_edge():
    tx = ops((0, "a", "b", 1_000, "transfer"), (60, "a", "b", 2_500, "transfer"))
    graph = build_graph(tx, ["a"], {})
    assert graph.number_of_edges() == 1
    assert graph.edges["a", "b"]["amount"] == 3_500
    assert graph.edges["a", "b"]["title"].startswith("2 опер., 3 500 ₽")


def test_node_kinds_and_tooltips():
    graph = build_graph(CHAIN, ["drop1"], {"drop1": 90}, drops={"drop1", "drop2"}, depth=1)
    assert graph.nodes["drop1"]["kind"] == SUSPICIOUS
    assert "Риск 90" in graph.nodes["drop1"]["title"]
    assert graph.nodes["drop2"]["kind"] == MISSED   # дроп по разметке, детектор пропустил
    assert graph.nodes["victim"]["kind"] == REGULAR
    assert graph.nodes[f"{EXTERNAL}:drop1"]["kind"] == INCOME
    assert graph.nodes["drop1"]["size"] > graph.nodes["drop2"]["size"]   # центральная карта крупнее


def test_card_labels_are_short():
    tx = ops((0, "2202 **** **** 4821 #a1b2c3", "5469 **** **** 0931", 50_000, "transfer"))
    graph = build_graph(tx, ["5469 **** **** 0931"], {"5469 **** **** 0931": 75})
    assert graph.nodes["5469 **** **** 0931"]["label"] == "•• 0931"
    assert graph.nodes["2202 **** **** 4821 #a1b2c3"]["label"] == "•• 4821 #a1b2c3"
    assert graph.nodes["2202 **** **** 4821 #a1b2c3"]["title"].startswith("2202 **** **** 4821 #a1b2c3")


def test_edge_limit_keeps_center_edges():
    rows = [(i, f"friend_{i}", "hub", 100 + i, "transfer") for i in range(20)]
    rows += [(30, "hub", "big", 1_000_000, "transfer"), (40, "big", "other", 900_000, "transfer")]
    graph = build_graph(ops(*rows), ["hub"], {"big": 70}, depth=2, max_edges=5, group_from=100)
    assert graph.number_of_edges() == 5
    assert all("hub" in edge for edge in graph.edges)


def test_many_senders_are_grouped():
    """Воронка: 12 отправителей сворачиваются в один узел, сумма и число операций сохраняются."""
    rows = [(i, f"victim_{i:02}", "hub", 10_000, "transfer") for i in range(12)]
    rows += [(60, "hub", "next", 115_000, "transfer"), (70, "lonely", "hub", 500, "transfer")]
    graph = build_graph(ops(*rows), ["hub"], {"hub": 85})
    group = "group:in:hub"
    assert not any(node.startswith("victim_") for node in graph)
    assert graph.nodes[group]["label"] == "13 карт"   # 12 жертв и ещё одна карта с одним переводом
    assert graph.edges[group, "hub"]["amount"] == 120_500
    assert graph.edges[group, "hub"]["title"].startswith("13 опер.")
    assert "victim_00" in graph.nodes[group]["title"]
    assert "next" in graph   # получатель один, сворачивать нечего


def test_grouping_keeps_suspicious_and_linked_cards():
    rows = [(i, f"victim_{i}", "hub", 10_000, "transfer") for i in range(6)]
    rows += [(10, "victim_0", "other", 5_000, "transfer")]   # у этой карты есть другая связь
    graph = build_graph(ops(*rows), ["hub", "other"], {"hub": 85, "victim_1": 70}, group_from=4)
    assert "victim_0" in graph and "victim_1" in graph
    assert graph.nodes["group:in:hub"]["label"] == "4 карты"


def test_layout_follows_the_money():
    """Деньги текут слева направо: жертва, дроп 1, дроп 2, банкомат."""
    tx = pd.concat([CHAIN, ops((400, EXTERNAL, "drop2", 2_000, "top_up"))], ignore_index=True)
    nodes, _ = vis_data(build_graph(tx, ["drop1"], RISK, depth=3), detailed=True)
    x = {node["id"]: node["x"] for node in nodes}
    assert x["victim"] < x["drop1"] < x["drop2"] < x[f"{ATM}:drop2"]
    # Пополнение стоит вплотную к своей карте, а не в самой левой колонке
    assert x[f"{EXTERNAL}:drop1"] == x["victim"]
    assert x[f"{EXTERNAL}:drop2"] == x["drop1"]


def test_labels_and_amounts_only_in_detailed_view():
    graph = build_graph(CHAIN, ["drop1"], RISK, depth=3)
    nodes, edges = vis_data(graph)
    labels = {node["id"]: node["label"] for node in nodes}
    assert labels["drop1"] == "drop1" and labels["victim"] == ""
    assert all("label" not in edge for edge in edges)

    nodes, edges = vis_data(graph, detailed=True)
    assert {node["id"]: node["label"] for node in nodes}["victim"] == "victim"
    assert {edge["label"] for edge in edges} >= {"100 тыс ₽", "3 000 ₽"}


def test_edge_colors_follow_the_important_end():
    _, edges = vis_data(build_graph(CHAIN, ["drop1"], RISK, depth=3))
    color = {(edge["from"], edge["to"]): edge["color"]["color"] for edge in edges}
    assert color["drop2", f"{ATM}:drop2"] == theme.rgba(theme.CASH, 0.6)
    assert color["victim", "drop1"] == theme.rgba(theme.RISK, 0.55)


def test_empty_graph():
    graph = build_graph(CHAIN, ["nobody"], RISK)
    assert graph.number_of_nodes() == 0
    page = to_html(graph)
    assert "vis.Network" in page
    assert page_data(page, "nodes") == []


def test_html_is_interactive_and_safe():
    card = "</script><script>alert(1)</script>&amp;"
    page = to_html(build_graph(ops((0, card, "b", 100, "transfer")), ["b"], {}), detailed=True)
    assert "vis.Network" in page
    assert "ResizeObserver" in page
    # Данные встроены в скрипт без единого <, > и &: закрыть тег или подставить сущность нечем
    embedded = re.search(r"const nodes = new vis.DataSet\((.*)\);", page).group(1)
    assert not set("<>&") & set(embedded)
    assert page_data(page, "nodes")[0]["title"] == card


def test_legend_lists_only_present_kinds():
    page = to_html(build_graph(CHAIN, ["drop1"], RISK, depth=3))
    legend = re.search(r'<div class="legend">(.*?)</div>', page).group(1)
    assert "подозрительная" in legend and "снятие наличных" in legend and "пополнение" in legend
    assert "пропущенный дроп" not in legend
    assert theme.BG in page   # тёмный фон страницы, как у дашборда


def test_layout_is_computed_in_advance():
    """Координаты задаются заранее, физика в браузере выключена, картинка не меняется от раза к разу."""
    graph = build_graph(CHAIN, ["drop1"], RISK, depth=3)
    page = to_html(graph)
    assert '"physics": {"enabled": false}' in page
    assert all("x" in node and "y" in node for node in page_data(page, "nodes"))
    assert page == to_html(graph)
