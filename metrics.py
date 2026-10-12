"""
Оценка точности детектора по разметке (ground_truth).

Проверка на многих синтетических наборах, чтобы хорошие цифры
не оказались удачей одного seed:
    python metrics.py --seeds 20
    python metrics.py --seeds 20 --difficulty hard
    python metrics.py --seeds 20 --difficulty hard --no-mitigation   # без смягчающих обстоятельств

В сложном режиме отчёт показывает ещё и ложные тревоги по жизненным ситуациям
честных людей, похожих на дропов (см. honest.py).
"""

import argparse
from dataclasses import dataclass

import pandas as pd

import generator
from detectors import Rules, detect
from honest import TITLES

SITUATION_COLUMNS = ["situation", "people", "flagged"]


@dataclass
class Evaluation:
    precision: float               # какая доля отмеченных карт - действительно дропы
    recall: float                  # какую долю дропов нашли
    f1: float
    by_scheme: pd.DataFrame        # полнота по каждой схеме
    false_positives: pd.DataFrame  # честные карты, отмеченные детектором, с причинами
    missed: pd.DataFrame           # пропущенные дроп-карты
    by_situation: pd.DataFrame     # честные люди, похожие на дропов: сколько из них отмечено


@dataclass
class Benchmark:
    per_seed: pd.DataFrame         # метрики по строке на seed
    by_scheme: pd.DataFrame        # полнота по схемам на всех наборах вместе
    by_situation: pd.DataFrame     # ложные тревоги по ситуациям на всех наборах вместе


def evaluate(suspicious: pd.DataFrame, truth: pd.DataFrame, lookalikes: pd.DataFrame | None = None) -> Evaluation:
    """Сравнивает список подозрительных карт с разметкой (колонки card и scheme).

    lookalikes - необязательная разметка честных людей (card, situation): по ней
    ложные тревоги раскладываются по жизненным ситуациям.
    """
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

    by_situation = pd.DataFrame(columns=SITUATION_COLUMNS)
    if lookalikes is not None and not lookalikes.empty:
        by_situation = (lookalikes.assign(flagged=lookalikes["card"].isin(flagged))
                                  .groupby("situation", sort=False)
                                  .agg(people=("card", "size"), flagged=("flagged", "sum"))
                                  .reset_index())

    return Evaluation(
        precision=precision,
        recall=recall,
        f1=f1,
        by_scheme=by_scheme.reset_index(),
        false_positives=suspicious[~suspicious["card"].isin(drops)].reset_index(drop=True),
        missed=truth[~truth["card"].isin(flagged)].reset_index(drop=True),
        by_situation=by_situation,
    )


def format_situations(by_situation: pd.DataFrame) -> list:
    """Строки отчёта: ложные тревоги по ситуациям, от самых частых."""
    table = by_situation.sort_values("flagged", ascending=False, kind="stable")
    return [f"  {TITLES.get(row.situation, row.situation)}: {row.flagged} из {row.people}"
            for row in table.itertuples()]


def format_report(result: Evaluation) -> str:
    """Отчёт о точности для вывода в консоль."""
    lines = [
        f"Precision: {result.precision:.1%}   Recall: {result.recall:.1%}   F1: {result.f1:.1%}",
        "",
        "По схемам:",
        result.by_scheme.to_string(index=False, formatters={"recall": "{:.0%}".format}),
    ]
    if not result.by_situation.empty:
        lines += ["", "Честные люди, принятые за дропов:"] + format_situations(result.by_situation)
    if not result.false_positives.empty:
        lines += ["", f"Ложные срабатывания ({len(result.false_positives)}):"]
        lines += [f"  {row.card}: {row.reason}" for row in result.false_positives.itertuples()]
    if not result.missed.empty:
        lines += ["", f"Пропущенные дроп-карты ({len(result.missed)}):"]
        lines += [f"  {row.card} ({row.scheme})" for row in result.missed.itertuples()]
    return "\n".join(lines)


def benchmark(seeds, n_clients: int = 200, difficulty: str = generator.EASY,
              rules: Rules | None = None) -> Benchmark:
    """Метрики детектора на синтетических наборах с разными seed."""
    rows, schemes, situations = [], [], []
    for seed in seeds:
        transactions, truth, lookalikes = generator.generate_labeled(seed, n_clients, difficulty)
        result = evaluate(detect(transactions, rules), truth, lookalikes)
        rows.append({"seed": seed, "precision": result.precision, "recall": result.recall, "f1": result.f1,
                     "false_positives": len(result.false_positives), "missed": len(result.missed)})
        schemes.append(result.by_scheme)
        situations.append(result.by_situation)

    by_scheme = pd.concat(schemes).groupby("scheme")[["drops", "caught"]].sum()
    by_scheme["recall"] = by_scheme["caught"] / by_scheme["drops"]
    by_situation = pd.concat(situations).groupby("situation", sort=False)[["people", "flagged"]].sum()
    return Benchmark(pd.DataFrame(rows), by_scheme.reset_index(), by_situation.reset_index())


def main():
    parser = argparse.ArgumentParser(description="Точность детектора на синтетических данных")
    parser.add_argument("--seeds", type=int, default=20, help="сколько наборов проверить (по умолчанию 20)")
    parser.add_argument("--clients", type=int, default=200, help="клиентов в каждом наборе (по умолчанию 200)")
    parser.add_argument("--difficulty", choices=generator.DIFFICULTIES, default=generator.EASY,
                        help="сложность синтетики, см. generator.py (по умолчанию easy)")
    parser.add_argument("--no-mitigation", action="store_true",
                        help="не учитывать смягчающие обстоятельства (знакомый отправитель, доход от организации)")
    args = parser.parse_args()

    result = benchmark(range(1, args.seeds + 1), args.clients, args.difficulty, Rules(mitigate=not args.no_mitigation))
    table = result.per_seed
    percent = "{:.1%}".format
    print(table.to_string(index=False, formatters={"precision": percent, "recall": percent, "f1": percent}))
    print(f"\nВ среднем на {args.seeds} наборах: precision {table['precision'].mean():.1%}, "
          f"recall {table['recall'].mean():.1%}, F1 {table['f1'].mean():.1%}")
    print(f"Худший набор: precision {table['precision'].min():.1%}, recall {table['recall'].min():.1%}")
    print("\nПолнота по схемам на всех наборах:")
    print(result.by_scheme.to_string(index=False, formatters={"recall": percent}))
    if not result.by_situation.empty:
        print(f"\nЧестные люди, принятые за дропов (ложных тревог всего: {table['false_positives'].sum()}):")
        print("\n".join(format_situations(result.by_situation)))


if __name__ == "__main__":
    main()
