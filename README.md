# drop_finder

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://drop-finder.streamlit.app)
[![Tests](https://github.com/PachkaKofe04/drop_finder/actions/workflows/tests.yml/badge.svg)](https://github.com/PachkaKofe04/drop_finder/actions/workflows/tests.yml)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Prototype anti-fraud tool for detecting money mule ("drop") cards from card transfer history.

A money mule card is used to receive stolen money and pass it on: to other cards or out through an ATM.
drop_finder looks for the patterns such cards leave in transaction data.

What is inside: a synthetic data generator with hidden mule schemes, a loader for real bank exports,
explainable rule-based detection with accuracy metrics, and a Streamlit dashboard with an interactive
money flow graph.

**Live demo:** [drop-finder.streamlit.app](https://drop-finder.streamlit.app) (synthetic data only).

## Quick start

Requires Python 3.11.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python generator.py --seed 42
python detectors.py data/transactions.csv --truth data/ground_truth.csv
streamlit run app.py
```

On Linux / macOS activate the environment with `source .venv/bin/activate`.

## Project structure

| File | Purpose |
|---|---|
| `generator.py` | Synthetic transactions with hidden mule schemes |
| `loader.py` | Loads and validates any CSV (synthetic or real) into the common format |
| `detectors.py` | Three detection rules, one row per suspicious card with a reason |
| `metrics.py` | Precision / recall against the ground truth, benchmark over many seeds |
| `flow_graph.py` | Money flow graph: networkx for the structure, pyvis for the picture |
| `app.py` | Streamlit dashboard |
| `tests/` | pytest suite |
| `data/` | CSV files, not tracked by git |

## Data format

Every other part of the project works with this format only, whatever the data source.

| Column | Meaning |
|---|---|
| `datetime` | Operation time, `2026-08-01 14:30:00` |
| `sender_card` | Sender card, `EXTERNAL` for top-ups |
| `receiver_card` | Receiver card, `ATM` for cash withdrawals |
| `amount` | Amount in rubles |
| `op_type` | `transfer`, `cash_withdrawal`, `top_up` (`other` if not recognized in real data) |

Cards in synthetic data are masked: `2202 **** **** 4821`.

## Synthetic data

```
python generator.py --seed 42 --clients 200
```

The same seed always gives the same files. Output:

- `data/transactions.csv` - 30 days of operations: ~200 regular clients
  (salaries, transfers to friends, ATM withdrawals) with mule schemes hidden among them;
- `data/ground_truth.csv` - mule cards and their scheme, to measure detection accuracy.

Hidden schemes:

| Scheme | Pattern |
|---|---|
| `transit` | Victim -> mule 1 -> mule 2 -> ATM. Each hop within 1-3 hours, amounts almost equal |
| `funnel` | 8-15 different cards send money to one card within 1-2 days, then it moves on |
| `fast_cashout` | A card receives a large amount and withdraws almost all of it within an hour |

Regular clients also include legitimate look-alikes, so that a detector has something to get wrong:
group money collections (many small transfers to one card) and cash withdrawals right after payday.

## Real data

`loader.py` accepts real bank exports in CSV:

- UTF-8 or Windows-1251, separated by `,`, `;` or tab;
- columns are matched by name, including Russian ones (`Дата операции`, `Сумма`, ...);
  other names can be mapped explicitly;
- dates like `2026-08-01 14:30:00` or `01.08.2026 14:30`, amounts like `1 234,56 руб.`;
- operation types are recognized by keywords (`Перевод`, `Снятие наличных`, `Пополнение`);
- rows that cannot be parsed are dropped with a warning.

Check a file:

```
python loader.py path/to/export.csv
```

Use it from code:

```python
from loader import load_transactions

df, warnings = load_transactions(
    "export.csv",
    column_map={"datetime": "Дата проводки", "amount": "Сумма в руб."},
    datetime_format="%d/%m/%Y %H:%M",  # only if the format is unusual
)
```

### Card numbers

Full card numbers never leave the loader. They are masked and tagged with a keyed hash:
`2202 **** **** 4821 #a1b2c3`. The tag keeps different cards with the same first and last
digits apart, and the number cannot be recovered from it without the key.

By default the key is random for each run. To get the same tags across runs,
set the `DROP_FINDER_SECRET` environment variable.

Card tokens or internal IDs are the best input: they are kept as is.
Already masked numbers (`2202 **** **** 4821`) are kept too, but different cards
with the same visible digits will then be merged into one.

### Handling real data

The `data/` folder is excluded from git. Do not commit or share real exports,
and follow your organization's rules for personal and payment card data.

## Detection

```
python detectors.py data/transactions.csv --truth data/ground_truth.csv
```

Three explainable rules. Thresholds live in `detectors.Rules` and are deliberately looser
than the generator's parameters, so the detector is not tuned to its own synthetic data.

| Rule | Fires when |
|---|---|
| `transit` | A transfer of 10 000+ arrives and 90-100% of it leaves the card within 6 hours. Cards passing money to each other are linked into chains with networkx |
| `funnel` | 8+ different senders within 48 hours, 50 000+ collected, and 80%+ of it leaves within the next 24 hours |
| `fast_cashout` | 30 000+ arrives and 80%+ of it is withdrawn in cash within an hour |

Every flagged card comes with a human-readable reason (in Russian), for example:

> Транзит: пришло 88 000 ₽ от 5536 \*\*\*\* \*\*\*\* 9197, за 1 ч 42 мин ушло 87 399 ₽ (99%) на 4276 \*\*\*\* \*\*\*\* 4734, цепочка из 2 транзитных карт

The risk score orders the list, it is not a probability: transit 70 (90 in a chain of transit cards),
fast cash-out 75, funnel 85, plus 5 for each additional rule. Without `--truth` the command works
on real exports and just saves the list to `data/suspicious.csv`.

### Results on synthetic data

`python metrics.py --seeds 20`

| | Precision | Recall | F1 |
|---|---|---|---|
| Seed 42 | 93.3% | 100% | 96.6% |
| Mean over 20 seeds | 94.6% | 100% | 97.1% |
| Worst seed | 87.5% | 100% | 93.3% |

All 17 false positives over 20 seeds come from the transit rule: a regular client receives
10-53 thousand rubles and happens to send about the same amount within a few hours. The legitimate look-alikes
(group collections, cash after payday) produce none. False positives get the lowest risk score (70),
while real transit chains get 90.

**Caveat:** the rules and the generator were written by the same author, so these numbers are optimistic.
Real data is noisier, and the thresholds will need tuning on it.

Performance: about 6 seconds for 1 million operations. Operations are indexed by (card, time) keys
and searched with binary search instead of row-by-row loops.

## Dashboard

```
streamlit run app.py
```

- data source: synthetic data (seed, number of clients) or your own CSV with an optional ground truth file;
- rule thresholds in the sidebar, the list and the metrics update immediately;
- suspicious cards with risk, rules and reasons, CSV download;
- card drill-down: every rule hit, the card's operations and an interactive money flow graph
  that follows the chain through other suspicious cards;
- network view of all suspicious cards and their direct links.

**Demo mode.** Uploading files is only available when the app is opened on `localhost`.
A public deployment, such as Streamlit Community Cloud, works on synthetic data only,
so that nobody uploads real customer data to a public server. To allow uploads on your own
server, set `DROP_FINDER_ALLOW_UPLOAD=1`.

## Tests

```
pip install -r requirements-dev.txt
pytest --cov
ruff check .
```

GitHub Actions runs the same on every push: lint, tests and coverage (the build fails below 90%).

The tests check that every hidden scheme matches its definition, that the legitimate
look-alikes stay below detection thresholds, that the loader handles messy real exports
(Windows-1251, `;`, Russian headers, mixed date formats, currency in amounts, full card numbers),
that each detection rule fires on its pattern and stays silent on hand-made near misses,
and that the dashboard builds without errors and reacts to its settings (Streamlit AppTest).
