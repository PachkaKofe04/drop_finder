"""Тесты графа движения денег."""

import pandas as pd

from flow_graph import COLOR_MISSED, COLOR_REGULAR, COLOR_SUSPICIOUS, REFIT_SCRIPT, build_graph, to_html
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
    assert graph.nodes[f"{ATM}:a"]["label"] == "Банкомат"


def test_operations_between_two_cards_are_one_edge():
    tx = ops((0, "a", "b", 1_000, "transfer"), (60, "a", "b", 2_500, "transfer"))
    graph = build_graph(tx, ["a"], {})
    assert graph.number_of_edges() == 1
    assert graph.edges["a", "b"]["amount"] == 3_500
    assert graph.edges["a", "b"]["title"].startswith("2 опер., 3 500 ₽")


def test_node_colors_and_tooltips():
    graph = build_graph(CHAIN, ["drop1"], {"drop1": 90}, drops={"drop1", "drop2"}, depth=1)
    assert graph.nodes["drop1"]["color"] == COLOR_SUSPICIOUS
    assert "Риск 90" in graph.nodes["drop1"]["title"]
    assert graph.nodes["drop2"]["color"] == COLOR_MISSED   # дроп по разметке, детектор пропустил
    assert graph.nodes["victim"]["color"] == COLOR_REGULAR
    assert graph.nodes["drop1"]["size"] > graph.nodes["drop2"]["size"]   # центральная карта крупнее


def test_edge_limit_keeps_center_edges():
    rows = [(i, f"friend_{i}", "hub", 100 + i, "transfer") for i in range(20)]
    rows += [(30, "hub", "big", 1_000_000, "transfer"), (40, "big", "other", 900_000, "transfer")]
    graph = build_graph(ops(*rows), ["hub"], {"big": 70}, depth=2, max_edges=5)
    assert graph.number_of_edges() == 5
    assert all("hub" in edge for edge in graph.edges)


def test_empty_graph():
    graph = build_graph(CHAIN, ["nobody"], RISK)
    assert graph.number_of_nodes() == 0
    assert "vis.Network" in to_html(graph)


def test_html_is_interactive_and_safe():
    tx = ops((0, "</script><script>alert(1)</script>", "b", 100, "transfer"))
    page = to_html(build_graph(tx, ["b"], {}))
    assert "vis.Network" in page
    assert REFIT_SCRIPT.strip() in page
    assert "<script>alert(1)" not in page
