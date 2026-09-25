"""
Веб-интерфейс детектора дропов.

Запуск:
    streamlit run app.py

Свои файлы можно загружать только при локальном запуске (localhost).
Публичная демо-версия работает на синтетике, чтобы туда не попали реальные
данные клиентов. Разрешить загрузку на своём сервере: DROP_FINDER_ALLOW_UPLOAD=1.

Данные из файлов (номера карт, причины) выводятся только в таблицы и как обычный
текст, не через markdown: иначе «2202 **** **** 4821» превратилось бы в жирный шрифт.
"""

import io
import os

import pandas as pd
import streamlit as st

import generator
from detectors import FAST_CASHOUT, FUNNEL, RULE_NAMES, TRANSIT, Rules, find_events, summarize
from flow_graph import COLOR_ATM, COLOR_EXTERNAL, COLOR_MISSED, COLOR_REGULAR, COLOR_SUSPICIOUS, build_graph, to_html
from loader import ATM, EXTERNAL, load_ground_truth, load_transactions
from metrics import evaluate

REPO_URL = "https://github.com/PachkaKofe04/drop_finder"
RULE_TITLES = {TRANSIT: "Транзит", FUNNEL: "Воронка", FAST_CASHOUT: "Быстрое обналичивание"}
SYNTHETIC, UPLOAD = "Синтетика", "Свой файл"


# --- Данные ------------------------------------------------------------------------

def uploads_allowed() -> bool:
    """Свои файлы - только при локальном запуске или с явным разрешением."""
    if os.environ.get("DROP_FINDER_ALLOW_UPLOAD") == "1":
        return True
    host = st.context.headers.get("Host") or ""
    return host.split(":")[0] in ("localhost", "127.0.0.1")


@st.cache_data(show_spinner="Генерирую синтетические данные...")
def synthetic_data(seed: int, clients: int):
    return generator.generate(seed, clients)


@st.cache_data(show_spinner="Читаю файл...")
def read_transactions(content: bytes):
    return load_transactions(io.BytesIO(content))


@st.cache_data(show_spinner="Читаю разметку...")
def read_truth(content: bytes):
    return load_ground_truth(io.BytesIO(content))


@st.cache_data(show_spinner="Ищу дропов...")
def run_detection(tx: pd.DataFrame, rules: Rules):
    events = find_events(tx, rules)
    return events, summarize(events)


def data_sidebar():
    """Источник данных. Возвращает (операции, разметка или None, предупреждения загрузчика)."""
    st.sidebar.header("Данные")
    allowed = uploads_allowed()
    source = st.sidebar.radio("Источник", [SYNTHETIC, UPLOAD] if allowed else [SYNTHETIC])
    if not allowed:
        st.sidebar.caption("В демо-версии доступна только синтетика. "
                           "Чтобы проверить свои данные, запустите проект локально (инструкция в README).")

    if source == SYNTHETIC:
        seed = st.sidebar.number_input("Seed", min_value=0, max_value=10_000, value=42)
        clients = st.sidebar.slider("Обычных клиентов", min_value=50, max_value=1000, value=200, step=50)
        transactions, truth = synthetic_data(seed, clients)
        return transactions, truth, []

    tx_file = st.sidebar.file_uploader("Операции, CSV", type=["csv", "txt"])
    truth_file = st.sidebar.file_uploader("Разметка дропов, CSV (необязательно)", type=["csv", "txt"])
    if tx_file is None:
        st.info("Загрузите CSV с операциями в боковой панели. Формат и примеры колонок описаны в README.")
        st.stop()
    try:
        transactions, warnings = read_transactions(tx_file.getvalue())
        truth = read_truth(truth_file.getvalue()) if truth_file else None
    except ValueError as error:
        st.error("Не удалось прочитать файл:")
        st.text(str(error))
        st.stop()
    return transactions, truth, warnings


def rules_sidebar() -> Rules:
    """Пороги правил. По умолчанию - как в detectors.Rules."""
    defaults = Rules()
    with st.sidebar.expander("Пороги правил"):
        st.caption("Транзит")
        transit_amount = st.number_input("Сумма перевода от, ₽", 0, None, int(defaults.transit_min_amount), 1_000)
        transit_hours = st.number_input("Ушло дальше за, часов", 1, 72, 6)
        transit_share = st.slider("Ушло не меньше, % от пришедшего", 50, 100, 90)

        st.caption("Воронка")
        funnel_senders = st.number_input("Разных отправителей от", 2, 100, defaults.funnel_min_senders)
        funnel_hours = st.number_input("За сколько часов", 1, 240, 48)
        funnel_total = st.number_input("Собрано от, ₽", 0, None, int(defaults.funnel_min_total), 5_000)
        funnel_share = st.slider("Ушло дальше за сутки, %", 50, 100, 80)

        st.caption("Быстрое обналичивание")
        cashout_amount = st.number_input("Поступление от, ₽", 0, None, int(defaults.cashout_min_amount), 5_000)
        cashout_minutes = st.number_input("Снято за, минут", 5, 1440, 60, 5)
        cashout_share = st.slider("Снято не меньше, %", 50, 100, 80)

    return Rules(
        transit_min_amount=transit_amount,
        transit_window=pd.Timedelta(hours=transit_hours),
        transit_min_share=transit_share / 100,
        funnel_window=pd.Timedelta(hours=funnel_hours),
        funnel_min_senders=funnel_senders,
        funnel_min_total=funnel_total,
        funnel_min_outflow_share=funnel_share / 100,
        cashout_min_amount=cashout_amount,
        cashout_window=pd.Timedelta(minutes=cashout_minutes),
        cashout_min_share=cashout_share / 100,
    )


# --- Разделы страницы -------------------------------------------------------------

def show_summary(tx: pd.DataFrame, suspicious: pd.DataFrame, result):
    n_cards = len((set(tx["sender_card"]) | set(tx["receiver_card"])) - {ATM, EXTERNAL})
    columns = st.columns(6 if result else 3)
    columns[0].metric("Операций", f"{len(tx):,}".replace(",", " "))
    columns[1].metric("Карт", f"{n_cards:,}".replace(",", " "))
    columns[2].metric("Подозрительных", len(suspicious))
    if result:
        columns[3].metric("Precision", f"{result.precision:.1%}",
                          help="Какая доля отмеченных карт - действительно дропы")
        columns[4].metric("Recall", f"{result.recall:.1%}", help="Какую долю дропов нашли")
        columns[5].metric("F1", f"{result.f1:.1%}", help="Среднее гармоническое precision и recall")


def show_table(suspicious: pd.DataFrame, drops: set, result):
    if suspicious.empty:
        st.success("Подозрительных карт не найдено.")
        return

    chosen = st.multiselect("Правила", RULE_NAMES, default=RULE_NAMES, format_func=RULE_TITLES.get)
    table = suspicious[suspicious["rules"].str.split(", ").apply(lambda rules: bool(set(rules) & set(chosen)))]
    table = table.assign(rules=table["rules"].str.split(", ").apply(
        lambda rules: ", ".join(RULE_TITLES[r] for r in rules)))
    if result:
        table = table.assign(drop=table["card"].isin(drops))

    # Высота по числу строк, чтобы список читался без внутренней прокрутки
    st.dataframe(table, hide_index=True, height=min(38 + 35 * len(table), 700), column_config={
        "card": st.column_config.TextColumn("Карта", width="medium"),
        "risk": st.column_config.ProgressColumn("Риск", min_value=0, max_value=100, format="%d", width="small"),
        "rules": st.column_config.TextColumn("Правила", width="medium"),
        "reason": st.column_config.TextColumn("Главная причина", width="large"),
        "first_seen": st.column_config.DatetimeColumn("Впервые", format="DD.MM.YYYY HH:mm", width="small"),
        "events": st.column_config.NumberColumn("Срабатываний", width="small"),
        "drop": st.column_config.CheckboxColumn("Дроп по разметке", width="small"),
    })
    st.download_button("Скачать список, CSV", suspicious.to_csv(index=False).encode("utf-8-sig"),
                       file_name="suspicious.csv", mime="text/csv")

    if result is not None and not result.missed.empty:
        st.subheader("Пропущенные дроп-карты")
        st.dataframe(result.missed, hide_index=True)


def show_legend(with_truth: bool):
    items = [(COLOR_SUSPICIOUS, "подозрительная"), (COLOR_REGULAR, "обычная карта"),
             (COLOR_ATM, "банкомат"), (COLOR_EXTERNAL, "пополнение")]
    if with_truth:
        items.insert(1, (COLOR_MISSED, "пропущенный дроп"))
    st.html(" &nbsp; ".join(f'<span style="color:{color}">&#9679;</span> {label}' for color, label in items))


def show_card(tx: pd.DataFrame, events: pd.DataFrame, suspicious: pd.DataFrame, drops: set):
    if suspicious.empty:
        st.info("Подозрительных карт нет, разбирать нечего.")
        return

    risk = dict(zip(suspicious["card"], suspicious["risk"], strict=True))
    card = st.selectbox("Карта", suspicious["card"], format_func=lambda c: f"{c}  (риск {risk[c]})")

    card_events = (events[events["card"] == card]
                   .sort_values(["score", "time"], ascending=[False, True])
                   .assign(rule=lambda e: e["rule"].map(RULE_TITLES)))
    st.dataframe(card_events[["rule", "score", "time", "reason"]], hide_index=True, column_config={
        "rule": st.column_config.TextColumn("Правило", width="small"),
        "score": st.column_config.NumberColumn("Оценка", width="small"),
        "time": st.column_config.DatetimeColumn("Когда", format="DD.MM.YYYY HH:mm", width="small"),
        "reason": st.column_config.TextColumn("Причина", width="large"),
    })

    graph_column, ops_column = st.columns([3, 2])
    with graph_column:
        st.caption("Движение денег вокруг карты: прямые связи и дальше по цепочке через подозрительные карты")
        st.iframe(to_html(build_graph(tx, [card], risk, drops, depth=3), height=480), height=500)
        show_legend(bool(drops))
    with ops_column:
        st.caption("Операции карты")
        ops = tx[(tx["sender_card"] == card) | (tx["receiver_card"] == card)]
        incoming = ops["receiver_card"] == card
        ops = ops.assign(direction=incoming.map({True: "приход", False: "расход"}),
                         counterparty=ops["sender_card"].where(incoming, ops["receiver_card"]))
        st.dataframe(ops[["datetime", "direction", "amount", "counterparty", "op_type"]], hide_index=True,
                     column_config={
                         "datetime": st.column_config.DatetimeColumn("Когда", format="DD.MM HH:mm"),
                         "direction": "Направление",
                         "amount": st.column_config.NumberColumn("Сумма, ₽", format="%.0f"),
                         "counterparty": "Контрагент",
                         "op_type": "Тип",
                     })


def show_network(tx: pd.DataFrame, suspicious: pd.DataFrame, drops: set):
    if suspicious.empty:
        st.info("Подозрительных карт нет.")
        return

    top = len(suspicious)
    if top > 10:
        top = st.slider("Сколько карт с наибольшим риском показать", 10, min(top, 150), min(top, 40))
    risk = dict(zip(suspicious["card"], suspicious["risk"], strict=True))
    graph = build_graph(tx, suspicious["card"].head(top), risk, drops, depth=1, max_edges=400)
    st.caption("Подозрительные карты и их прямые связи. Узлы можно двигать, при наведении видны подробности.")
    st.iframe(to_html(graph, height=620), height=640)
    show_legend(bool(drops))


def show_about():
    st.markdown(f"""
Детектор ищет **дроп-карты**: карты, через которые прогоняют и обналичивают похищенные деньги.
Правила объяснимые: у каждой найденной карты есть причина и операции-доказательства.

| Правило | Когда срабатывает |
|---|---|
| Транзит | Пришёл перевод от 10 000 ₽, и за 6 часов с карты ушло 90-100% этой суммы |
| Воронка | За 48 часов переводы от 8+ разных карт, собрано от 50 000 ₽, и за следующие сутки ушло 80%+ |
| Быстрое обналичивание | Пришло от 30 000 ₽, и за час снято наличными 80%+ |

Транзитные карты, передающие деньги друг другу, склеиваются в цепочки.
Пороги можно менять в боковой панели и сразу видеть, как меняются результат и точность.

В синтетических данных спрятаны три схемы, а среди обычных клиентов есть **ловушки**:
складчина (много мелких переводов на одну карту) и снятие наличных сразу после зарплаты.

Код, тесты и описание: [{REPO_URL}]({REPO_URL})
""")


# --- Страница ----------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Детектор дропов", layout="wide")
    st.title("Детектор дропов")
    st.caption("Прототип антифрод-инструмента: поиск дроп-карт по истории переводов")

    transactions, truth, warnings = data_sidebar()
    rules = rules_sidebar()
    if warnings:
        st.warning("При загрузке часть строк исправлена или отброшена:")
        st.text("\n".join(warnings))

    events, suspicious = run_detection(transactions, rules)
    result = evaluate(suspicious, truth) if truth is not None else None
    drops = set(truth["card"]) if truth is not None else set()

    show_summary(transactions, suspicious, result)
    tab_list, tab_card, tab_network, tab_about = st.tabs(
        ["Подозрительные карты", "Разбор карты", "Граф связей", "Как это работает"])
    with tab_list:
        show_table(suspicious, drops, result)
    with tab_card:
        show_card(transactions, events, suspicious, drops)
    with tab_network:
        show_network(transactions, suspicious, drops)
    with tab_about:
        show_about()


main()
