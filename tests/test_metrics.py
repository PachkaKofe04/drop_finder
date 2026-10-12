"""Тесты метрик и точности детектора на синтетике."""

import sys

import pandas as pd
import pytest

import generator
import metrics
from detectors import Rules, detect
from metrics import benchmark, evaluate, format_report


def cards(*names) -> pd.DataFrame:
    return pd.DataFrame({"card": list(names), "reason": [f"причина {n}" for n in names]})


TRUTH = pd.DataFrame({"card": ["a", "b", "c", "d"], "scheme": ["transit", "transit", "funnel", "fast_cashout"]})


def test_evaluate_counts():
    result = evaluate(cards("a", "b", "x"), TRUTH)
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(0.5)
    assert result.f1 == pytest.approx(4 / 7)
    assert list(result.false_positives["card"]) == ["x"]
    assert list(result.missed["card"]) == ["c", "d"]
    recall = dict(zip(result.by_scheme["scheme"], result.by_scheme["recall"], strict=True))
    assert recall == {"fast_cashout": 0.0, "funnel": 0.0, "transit": 1.0}


def test_nothing_flagged():
    result = evaluate(cards(), TRUTH)
    assert (result.precision, result.recall, result.f1) == (0.0, 0.0, 0.0)


def test_false_alarms_by_situation():
    lookalikes = pd.DataFrame({"card": ["x", "y", "z"], "situation": ["wedding", "wedding", "mortgage"]})
    result = evaluate(cards("a", "x"), TRUTH, lookalikes)
    counts = {row.situation: (row.people, row.flagged) for row in result.by_situation.itertuples()}
    assert counts == {"wedding": (2, 1), "mortgage": (1, 0)}
    report = format_report(result)
    assert "Честные люди, принятые за дропов:" in report
    assert "Свадьба, подарки переводом: 1 из 2" in report
    assert evaluate(cards("a"), TRUTH).by_situation.empty


def test_report_lists_errors():
    report = format_report(evaluate(cards("a", "x"), TRUTH))
    assert "Precision: 50.0%" in report
    assert "x: причина x" in report
    assert "c (funnel)" in report


def test_detector_on_seed_42(synthetic):
    transactions, truth = synthetic
    result = evaluate(detect(transactions), truth)
    assert result.recall == 1.0
    assert result.precision >= 0.9


def test_detector_on_several_seeds():
    """Хорошие цифры не должны быть удачей одного seed."""
    result = benchmark(range(1, 6))
    table, by_scheme = result.per_seed, result.by_scheme
    assert list(table["seed"]) == [1, 2, 3, 4, 5]
    assert (table["recall"] == 1.0).all()
    assert table["precision"].min() >= 0.85
    assert list(by_scheme["scheme"]) == ["fast_cashout", "funnel", "transit"]
    assert (by_scheme["caught"] == by_scheme["drops"]).all()
    assert result.by_situation.empty   # в простом режиме честных людей с разметкой нет


def test_benchmark_hard():
    """В сложном режиме полнота считается по всем шести схемам и складывается по всем наборам."""
    result = benchmark(range(1, 3), difficulty=generator.HARD)
    table, by_scheme = result.per_seed, result.by_scheme
    assert set(by_scheme["scheme"]) == {"transit", "funnel", "fast_cashout", "split", "warmup", "p2p"}
    assert by_scheme["drops"].sum() - by_scheme["caught"].sum() == table["missed"].sum()
    assert (by_scheme["recall"] == by_scheme["caught"] / by_scheme["drops"]).all()
    # Ложные тревоги на честных людях входят в общее число ложных срабатываний
    assert result.by_situation["people"].sum() == 2 * (200 // 5 + 200 // 50 + 200 // 100)
    assert result.by_situation["flagged"].sum() <= table["false_positives"].sum()


def test_mitigation_cuts_false_alarms_but_not_recall():
    """Смягчающие обстоятельства убирают ложные тревоги на честных людях, а дропов не прячут."""
    with_mitigation = benchmark(range(1, 4), difficulty=generator.HARD)
    without = benchmark(range(1, 4), difficulty=generator.HARD, rules=Rules(mitigate=False))
    assert with_mitigation.by_situation["flagged"].sum() < without.by_situation["flagged"].sum()
    assert (with_mitigation.per_seed["recall"] == without.per_seed["recall"]).all()


def test_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["metrics.py", "--seeds", "2", "--clients", "60"])
    metrics.main()
    out = capsys.readouterr().out
    assert "В среднем на 2 наборах" in out
    assert "Полнота по схемам" in out


def test_cli_hard(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["metrics.py", "--seeds", "1", "--clients", "50", "--difficulty", "hard",
                                      "--no-mitigation"])
    metrics.main()
    out = capsys.readouterr().out
    assert "split" in out
    assert "Честные люди, принятые за дропов" in out
