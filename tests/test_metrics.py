"""Тесты метрик и точности детектора на синтетике."""

import sys

import pandas as pd
import pytest

import metrics
from detectors import detect
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
    table = benchmark(range(1, 6))
    assert (table["recall"] == 1.0).all()
    assert table["precision"].min() >= 0.85


def test_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["metrics.py", "--seeds", "2", "--clients", "60"])
    metrics.main()
    assert "В среднем на 2 наборах" in capsys.readouterr().out
