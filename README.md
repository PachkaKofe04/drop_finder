# drop_finder

[![Tests](https://github.com/PachkaKofe04/drop_finder/actions/workflows/tests.yml/badge.svg)](https://github.com/PachkaKofe04/drop_finder/actions/workflows/tests.yml)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Prototype anti-fraud tool for detecting money mule ("drop") cards from card transfer history.

A money mule card is used to receive stolen money and pass it on: to other cards or out through an ATM.
drop_finder looks for the patterns such cards leave in transaction data.

**Current stage:** project structure, a synthetic data generator with hidden mule schemes,
and a loader that brings both synthetic data and real bank exports to one format.
Detection and the web interface come next.

## Quick start

Requires Python 3.11.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python generator.py --seed 42
streamlit run app.py
```

On Linux / macOS activate the environment with `source .venv/bin/activate`.

## Project structure

| File | Purpose |
|---|---|
| `generator.py` | Synthetic transactions with hidden mule schemes |
| `loader.py` | Loads and validates any CSV (synthetic or real) into the common format |
| `app.py` | Streamlit web interface (placeholder for now) |
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

## Tests

```
pip install -r requirements-dev.txt
pytest --cov
ruff check .
```

GitHub Actions runs the same on every push: lint, tests and coverage (the build fails below 90%).

The tests check that every hidden scheme matches its definition, that the legitimate
look-alikes stay below detection thresholds, and that the loader handles messy real exports:
Windows-1251, `;`, Russian headers, mixed date formats, currency in amounts, full card numbers.
