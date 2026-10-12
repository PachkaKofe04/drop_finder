"""
Оценка точности детектора по разметке (ground_truth).

Проверка на многих синтетических наборах, чтобы хорошие цифры
не оказались удачей одного seed:
    python metrics.py --seeds 20
    python metrics.py --seeds 20 --difficulty hard
"""

import argparse
from dataclasses import dataclass

import pandas as pd

import generator
from detectors import detect


@dataclass
class Evaluation:
    precision: float               # какая доля отмеченных карт - действительно дропы
    recall: float                  # какую долю дропов нашли
    f1: float
    by_scheme: pd.DataFrame        # полнота по каждой схеме
    false_positives: pd.DataFrame  # честные карты, отмеченные детектором, с причинами
    missed: pd.DataFrame           # пропущенные дроп-карты


def evaluate(suspicious: pd.DataFrame, truth: pd.DataFrame) -> Evaluation:
    """Сравнивает список подозрительных карт с разметкой (колонки card и scheme)."""
    flagged = set(suspicious["card"])
    drops = set(truth["card"])
    caught = len(flagged & drops)

    precision = caught / len(flagged) if flagged else 0.0
    recall = caught / len(drops) if drops else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    by_scheme = (truth.assign(caught=truth["card"].isin(flagged))
                      .groupby("scheme")
                      .agg(drops=("card", "size"), caught=("caught", "sum")))
    by_scheme["recall"] = by_scheme["caught"] / by_scheme["drops"]

    return Evaluation(
        precision=precision,
        recall=recall,
        f1=f1,
        by_scheme=by_scheme.reset_index(),
        false_positives=suspicious[~suspicious["card"].isin(drops)].reset_index(drop=True),
        missed=truth[~truth["card"].isin(flagged)].reset_index(drop=True),
    )


def format_report(result: Evaluation) -> str:
    """Отчёт о точности для вывода в консоль."""
    lines = [
        f"Precision: {result.precision:.1%}   Recall: {result.recall:.1%}   F1: {result.f1:.1%}",
        "",
        "По схемам:",
        result.by_scheme.to_string(index=False, formatters={"recall": "{:.0%}".format}),
    ]
    if not result.false_positives.empty:
        lines += ["", f"Ложные срабатывания ({len(result.false_positives)}):"]
        lines += [f"  {row.card}: {row.reason}" for row in result.false_positives.itertuples()]
    if not result.missed.empty:
        lines += ["", f"Пропущенные дроп-карты ({len(result.missed)}):"]
        lines += [f"  {row.card} ({row.scheme})" for row in result.missed.itertuples()]
    return "\n".join(lines)


def benchmark(seeds, n_clients: int = 200, difficulty: str = generator.EASY) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Метрики детектора на синтетических наборах с разными seed.

    Возвращает (по строке на seed, полнота по схемам на всех наборах вместе).
    """
    rows, schemes = [], []
    for seed in seeds:
        transactions, truth = generator.generate(seed, n_clients, difficulty)
        result = evaluate(detect(transactions), truth)
        rows.append({"seed": seed, "precision": result.precision, "recall": result.recall, "f1": result.f1,
                     "false_positives": len(result.false_positives), "missed": len(result.missed)})
        schemes.append(result.by_scheme)

    by_scheme = pd.concat(schemes).groupby("scheme")[["drops", "caught"]].sum()
    by_scheme["recall"] = by_scheme["caught"] / by_scheme["drops"]
    return pd.DataFrame(rows), by_scheme.reset_index()


def main():
    parser = argparse.ArgumentParser(description="Точность детектора на синтетических данных")
    parser.add_argument("--seeds", type=int, default=20, help="сколько наборов проверить (по умолчанию 20)")
    parser.add_argument("--clients", type=int, default=200, help="клиентов в каждом наборе (по умолчанию 200)")
    parser.add_argument("--difficulty", choices=generator.DIFFICULTIES, default=generator.EASY,
                        help="сложность синтетики, см. generator.py (по умолчанию easy)")
    args = parser.parse_args()

    table, by_scheme = benchmark(range(1, args.seeds + 1), args.clients, args.difficulty)
    percent = "{:.1%}".format
    print(table.to_string(index=False, formatters={"precision": percent, "recall": percent, "f1": percent}))
    print(f"\nВ среднем на {args.seeds} наборах: precision {table['precision'].mean():.1%}, "
          f"recall {table['recall'].mean():.1%}, F1 {table['f1'].mean():.1%}")
    print(f"Худший набор: precision {table['precision'].min():.1%}, recall {table['recall'].min():.1%}")
    print("\nПолнота по схемам на всех наборах:")
    print(by_scheme.to_string(index=False, formatters={"recall": percent}))


if __name__ == "__main__":
    main()
