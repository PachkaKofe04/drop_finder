"""
Загрузка транзакций из CSV: синтетических (из generator.py) или реальных.

Остальные модули проекта получают данные только через load_transactions(),
поэтому им не важно, откуда пришёл файл: из генератора или из выгрузки
банковской системы.

Что делает загрузчик:
  1. Читает CSV в кодировке UTF-8 или Windows-1251, с разделителем «,», «;» или табуляцией.
  2. Находит нужные колонки по названию (в т.ч. русскому) или по явному column_map.
  3. Приводит типы: дата/время, сумма (понимает «1 234,56 ₽»), тип операции.
  4. Маскирует полные номера карт, чтобы они не попадали в интерфейс и отчёты.
  5. Отбрасывает битые строки и сообщает, сколько их и почему.

Формат на выходе (DataFrame, отсортирован по времени):
  datetime       datetime64  - момент операции
  sender_card    str         - карта отправителя (для пополнения - EXTERNAL)
  receiver_card  str         - карта получателя (для снятия наличных - ATM)
  amount         float       - сумма, всегда > 0
  op_type        str         - transfer | cash_withdrawal | top_up | other

Проверить файл из командной строки:
    python loader.py путь/к/файлу.csv
"""

import hashlib
import hmac
import io
import os
import re
import secrets
import sys
from pathlib import Path

import pandas as pd

# --- Формат данных, общий для всего проекта ---------------------------------

COLUMNS = ["datetime", "sender_card", "receiver_card", "amount", "op_type"]
OP_TYPES = ["transfer", "cash_withdrawal", "top_up"]
OTHER = "other"        # тип операции, который не удалось распознать
ATM = "ATM"            # «получатель» при снятии наличных
EXTERNAL = "EXTERNAL"  # «отправитель» при пополнении (зарплата, другой банк, наличные)

# Возможные названия колонок в реальных выгрузках (регистр не важен).
COLUMN_ALIASES = {
    "datetime": ["datetime", "date", "timestamp", "дата", "дата операции",
                 "дата и время", "время операции"],
    "sender_card": ["sender_card", "sender", "from_card", "карта отправителя",
                    "отправитель", "карта списания"],
    "receiver_card": ["receiver_card", "receiver", "to_card", "карта получателя",
                      "получатель", "карта зачисления"],
    "amount": ["amount", "sum", "сумма", "сумма операции"],
    "op_type": ["op_type", "type", "operation", "тип операции", "тип", "операция"],
}

GROUND_TRUTH_ALIASES = {
    "card": ["card", "карта", "номер карты"],
    "scheme": ["scheme", "схема", "тип схемы"],
}

# Ключевые слова для распознавания типа операции. Порядок важен:
# «Пополнение наличными» должно стать top_up, а не снятием.
OP_TYPE_KEYWORDS = [
    ("top_up", ["top_up", "topup", "deposit", "пополн", "зачисл", "взнос"]),
    ("cash_withdrawal", ["cash_withdrawal", "withdraw", "atm", "сняти", "выдача", "банкомат"]),
    ("transfer", ["transfer", "p2p", "перевод"]),
]

# Полный номер карты: 13-19 цифр (после удаления пробелов и дефисов).
PAN_PATTERN = re.compile(r"\d{13,19}")

# Частые форматы даты в российских выгрузках (после ISO)
RU_DATETIME_FORMATS = ["%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"]

# Обозначения валюты в сумме и само число после очистки
CURRENCY_PATTERN = re.compile(r"руб\.?|р\.?|rub|rur|₽", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"-?\d+(\.\d+)?")


# --- Номера карт --------------------------------------------------------------

_secret_key = None


def _get_secret_key() -> bytes:
    """Ключ для метки карты (см. normalize_card).

    Если задана переменная окружения DROP_FINDER_SECRET, метки одинаковы
    между запусками. Иначе ключ случайный и живёт, пока работает программа.
    """
    global _secret_key
    if _secret_key is None:
        env_value = os.environ.get("DROP_FINDER_SECRET")
        _secret_key = env_value.encode() if env_value else secrets.token_bytes(32)
    return _secret_key


def normalize_card(value) -> str:
    """Приводит идентификатор карты к безопасному виду.

    Полный номер маскируется: '2202 **** **** 4821 #a1b2c3'.
    Метка #... - это HMAC от полного номера. Благодаря ей разные карты с
    одинаковыми первыми и последними цифрами не склеиваются в одну, а
    восстановить номер по метке без секретного ключа нельзя.
    Всё остальное (токены, внутренние ID, уже маскированные номера,
    ATM, EXTERNAL) остаётся как есть.
    """
    text = str(value).strip()
    digits = re.sub(r"[\s-]", "", text)
    if PAN_PATTERN.fullmatch(digits):
        tag = hmac.new(_get_secret_key(), digits.encode(), hashlib.sha256).hexdigest()[:6]
        return f"{digits[:4]} **** **** {digits[-4:]} #{tag}"
    return text


# --- Вспомогательные функции -------------------------------------------------

def _normalize_name(name) -> str:
    return str(name).strip().lower().replace("ё", "е")


def _map_unique(values: pd.Series, func) -> pd.Series:
    """Применяет func к каждому уникальному значению один раз.

    На реальных выгрузках в миллионы строк это намного быстрее, чем apply.
    """
    return values.map({value: func(value) for value in values.unique()})


def _read_csv(source) -> pd.DataFrame:
    """Читает CSV из пути или из файлового объекта (например, загруженного в Streamlit)."""
    raw = source.read() if hasattr(source, "read") else Path(source).read_bytes()

    if isinstance(raw, bytes):
        # Выгрузки из Excel и банковских систем часто в Windows-1251
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                raw = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("Не удалось определить кодировку (ожидается UTF-8 или Windows-1251)")

    # Разделитель определяем по строке заголовка
    header = raw.split("\n", 1)[0]
    separator = max([",", ";", "\t"], key=header.count)

    # Всё читаем как текст: номера карт не должны превращаться в числа
    return pd.read_csv(io.StringIO(raw), sep=separator, dtype=str, keep_default_na=False)


def _rename_columns(df: pd.DataFrame, aliases: dict, column_map: dict | None) -> pd.DataFrame:
    """Находит нужные колонки и переименовывает их в стандартные имена.

    column_map - явное соответствие «наша колонка → колонка в файле»,
    оно важнее автоматического поиска по aliases.
    """
    column_map = column_map or {}
    by_name = {_normalize_name(col): col for col in df.columns}

    rename, missing = {}, []
    for target, names in aliases.items():
        if target in column_map:
            source_col = column_map[target]
        else:
            source_col = next((by_name[_normalize_name(n)] for n in names
                               if _normalize_name(n) in by_name), None)
        if source_col in df.columns:
            rename[source_col] = target
        else:
            missing.append(target)

    if missing:
        raise ValueError(
            f"Не найдены колонки: {missing}. Колонки в файле: {list(df.columns)}. "
            "Укажите соответствие через column_map, например "
            "{'datetime': 'Дата проводки'}."
        )
    return df.rename(columns=rename)


def _parse_datetime(values: pd.Series, datetime_format: str | None) -> pd.Series:
    if datetime_format:
        return pd.to_datetime(values, format=datetime_format, errors="coerce")

    # Сначала строгий ISO (2026-08-01 14:30:00), потом частые «русские» форматы.
    # Разбор по известному формату быстрый, поэтому пробуем их по очереди.
    parsed = pd.to_datetime(values, format="ISO8601", errors="coerce")
    for fmt in RU_DATETIME_FORMATS:
        failed = parsed.isna() & values.ne("")
        if not failed.any():
            return parsed
        parsed[failed] = pd.to_datetime(values[failed], format=fmt, errors="coerce")

    # Всё, что осталось, разбираем медленно, построчно, считая первым числом день
    failed = parsed.isna() & values.ne("")
    if failed.any():
        parsed[failed] = pd.to_datetime(values[failed], format="mixed", dayfirst=True, errors="coerce")
    return parsed


def _parse_amount(value) -> float:
    """«1 234,56 руб.» → 1234.56, нераспознанное → NaN.

    Формат «1,234.56» намеренно не угадываем: лучше отбросить строку
    с предупреждением, чем молча получить сумму в 1000 раз меньше.
    """
    text = CURRENCY_PATTERN.sub("", re.sub(r"\s", "", str(value))).replace(",", ".")
    if not NUMBER_PATTERN.fullmatch(text):
        return float("nan")
    # В выписках списания бывают со знаком минус - берём по модулю
    return abs(float(text))


def _normalize_op_type(value) -> str:
    text = _normalize_name(value)
    if text in OP_TYPES:
        return text
    for op_type, keywords in OP_TYPE_KEYWORDS:
        if any(word in text for word in keywords):
            return op_type
    return OTHER


# --- Публичные функции -------------------------------------------------------

def load_transactions(source, column_map: dict | None = None,
                      datetime_format: str | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Загружает транзакции и приводит их к формату проекта.

    source          - путь к CSV или файловый объект (например, из st.file_uploader)
    column_map      - соответствие «наша колонка → колонка в файле», если
                      автоопределение не сработало, например
                      {"datetime": "Дата проводки", "amount": "Сумма в руб."}
    datetime_format - формат даты, если он нестандартный, например "%d/%m/%Y %H:%M"

    Возвращает (df, warnings): таблицу в формате проекта и список
    предупреждений о том, что было исправлено или отброшено.
    """
    raw = _rename_columns(_read_csv(source), COLUMN_ALIASES, column_map)
    warnings = []

    df = pd.DataFrame({
        "datetime": _parse_datetime(raw["datetime"], datetime_format),
        "sender_card": _map_unique(raw["sender_card"], normalize_card),
        "receiver_card": _map_unique(raw["receiver_card"], normalize_card),
        "amount": _map_unique(raw["amount"], _parse_amount),
        "op_type": _map_unique(raw["op_type"], _normalize_op_type),
    })

    # В реальных выгрузках у снятий часто нет «получателя», а у пополнений - «отправителя»
    df.loc[df["op_type"].eq("cash_withdrawal") & df["receiver_card"].eq(""), "receiver_card"] = ATM
    df.loc[df["op_type"].eq("top_up") & df["sender_card"].eq(""), "sender_card"] = EXTERNAL

    unknown = df["op_type"].eq(OTHER)
    if unknown.any():
        examples = sorted(raw.loc[unknown, "op_type"].unique())[:10]
        warnings.append(f"Строк с нераспознанным типом операции ({OTHER}): {unknown.sum()}. "
                        f"Примеры: {examples}")

    # Отбрасываем строки, которые нельзя анализировать
    problems = {
        "дата не распознана": df["datetime"].isna(),
        "сумма не распознана или равна 0": df["amount"].isna() | df["amount"].eq(0),
        "нет отправителя или получателя": df["sender_card"].eq("") | df["receiver_card"].eq(""),
    }
    bad = pd.Series(False, index=df.index)
    for reason, mask in problems.items():
        if mask.any():
            warnings.append(f"Строк с проблемой «{reason}»: {mask.sum()}")
        bad |= mask
    if bad.any():
        warnings.append(f"Всего отброшено строк: {bad.sum()} из {len(df)}")

    df = df[~bad].sort_values("datetime", kind="stable").reset_index(drop=True)
    if df.empty:
        raise ValueError("После проверки не осталось ни одной корректной строки")
    return df, warnings


def load_ground_truth(source, column_map: dict | None = None) -> pd.DataFrame:
    """Загружает разметку: какие карты - дропы. Нужна только для оценки точности.

    Обязательные колонки: card и scheme, остальные сохраняются как есть.
    Номера карт нормализуются так же, как в load_transactions(),
    поэтому разметка сопоставляется с транзакциями.
    """
    df = _rename_columns(_read_csv(source), GROUND_TRUTH_ALIASES, column_map)
    df["card"] = _map_unique(df["card"], normalize_card)
    return df[df["card"].ne("")].reset_index(drop=True)


if __name__ == "__main__":
    # Быстрая проверка файла: python loader.py data/transactions.csv
    if len(sys.argv) != 2:
        sys.exit("Использование: python loader.py путь/к/файлу.csv")

    transactions, problems_found = load_transactions(sys.argv[1])
    for line in problems_found:
        print("Внимание:", line)
    cards = pd.concat([transactions["sender_card"], transactions["receiver_card"]]).nunique()
    print(f"Строк: {len(transactions)}, уникальных карт (вкл. {ATM}/{EXTERNAL}): {cards}")
    print(f"Период: {transactions['datetime'].min()} - {transactions['datetime'].max()}")
    print("Типы операций:")
    print(transactions["op_type"].value_counts().to_string())
