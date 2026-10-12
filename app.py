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

import base64
import io
import os
from pathlib import Path

import pandas as pd
import streamlit as st

import generator
import theme
from detectors import FAST_CASHOUT, FUNNEL, MITIGATION, RULE_NAMES, TRANSIT, Rules, find_events, summarize
from flow_graph import build_graph, to_html
from honest import TITLES as SITUATION_TITLES
from loader import ATM, EXTERNAL, load_ground_truth, load_transactions
from metrics import evaluate
from tracing import to_sankey, trace

REPO_URL = "https://github.com/PachkaKofe04/drop_finder"
RULE_TITLES = {TRANSIT: "Транзит", FUNNEL: "Воронка", FAST_CASHOUT: "Быстрое обналичивание"}
SCHEME_TITLES = {**RULE_TITLES, "split": "Разветвление", "warmup": "Прогрев", "p2p": "P2P-дроп"}
DIFFICULTY_TITLES = {generator.EASY: "Простая", generator.HARD: "Сложная"}
OP_TITLES = {"transfer": "перевод", "cash_withdrawal": "снятие", "top_up": "пополнение", "payment": "оплата",
             "other": "другое"}
PARTY_TITLES = {EXTERNAL: "Пополнение", ATM: "Банкомат"}
SYNTHETIC, UPLOAD = "Синтетика", "Свой файл"
CSS = Path(__file__).parent / "assets" / "app.css"
ROW_HEIGHT = 35

# Знак продукта: деньги расходятся из одной карты по двум. Картинкой, потому что
# встроенный <svg> st.html вырезает.
EMBLEM = "data:image/svg+xml;base64," + base64.b64encode(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{theme.ACCENT}" '
    'stroke-width="1.8" stroke-linecap="round"><circle cx="5" cy="12" r="2.4"/><circle cx="19" cy="5.5" r="2.4"/>'
    '<circle cx="19" cy="18.5" r="2.4"/><path d="M7.3 11 16.7 6.6M7.3 13l9.4 4.4"/></svg>'.encode()).decode()


# --- Данные ------------------------------------------------------------------------

def uploads_allowed() -> bool:
    """Свои файлы - только при локальном запуске или с явным разрешением."""
    if os.environ.get("DROP_FINDER_ALLOW_UPLOAD") == "1":
        return True
    host = st.context.headers.get("Host") or ""
    return host.split(":")[0] in ("localhost", "127.0.0.1")


@st.cache_data(show_spinner="Генерирую синтетические данные...")
def synthetic_data(seed: int, clients: int, difficulty: str):
    return generator.generate_labeled(seed, clients, difficulty)


@st.cache_data(show_spinner="Читаю файл...")
def read_transactions(content: bytes):
    return load_transactions(io.BytesIO(content))


@st.cache_data(show_spinner="Читаю разметку...")
def read_truth(content: bytes):
    return load_ground_truth(io.BytesIO(content))


@st.cache_data(show_spinner="Ищу дропов...")
def run_detection(tx: pd.DataFrame, rules: Rules):
    events = find_events(tx, rules)
    return events, summarize(events, rules.min_risk)


def data_sidebar():
    """Источник данных.

    Возвращает (операции, разметка дропов или None, разметка честных людей или None,
    предупреждения загрузчика). Честные люди с разметкой есть только в сложной синтетике.
    """
    st.sidebar.html(f'<div class="df-wordmark"><img src="{EMBLEM}" alt=""><div><b>DROP FINDER</b>'
                    '<small>антифрод-прототип</small></div></div>')
    st.sidebar.header("Данные")
    allowed = uploads_allowed()
    source = st.sidebar.radio("Источник", [SYNTHETIC, UPLOAD] if allowed else [SYNTHETIC])
    if not allowed:
        st.sidebar.caption("В демо-версии доступна только синтетика. "
                           "Чтобы проверить свои данные, запустите проект локально (инструкция в README).")

    if source == SYNTHETIC:
        seed = st.sidebar.number_input("Seed", min_value=0, max_value=10_000, value=42)
        clients = st.sidebar.slider("Обычных клиентов", min_value=50, max_value=1000, value=200, step=50)
        difficulty = st.sidebar.radio(
            "Сложность синтетики", generator.DIFFICULTIES, format_func=DIFFICULTY_TITLES.get, horizontal=True,
            help="Простая: схемы укладываются в пороги правил. Сложная: добавлены схемы, которые эти пороги "
                 "обходят (разветвление, прогрев, P2P-дроп), и честные люди в ситуациях, похожих на дропов "
                 "(ипотека, свадьба, продажа машины...). Подробнее - «Как это работает».")
        transactions, truth, lookalikes = synthetic_data(seed, clients, difficulty)
        return transactions, truth, lookalikes, []

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
    return transactions, truth, None, warnings


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

        st.caption("Смягчающие обстоятельства")
        mitigate = st.checkbox("Учитывать", value=defaults.mitigate,
                               help="Знакомый отправитель (операции между картами были и раньше) или доход "
                                    "от организации, которая платит многим (работодатель, Соцфонд), "
                                    f"снижают оценку срабатывания на {MITIGATION}. Выключите, чтобы сравнить.")

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
        mitigate=mitigate,
    )


# --- Разделы страницы -------------------------------------------------------------

def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def scheme_titles(schemes: pd.Series) -> pd.Series:
    """Названия схем по-русски; незнакомые (из своей разметки) остаются как есть."""
    return schemes.map(SCHEME_TITLES).fillna(schemes)


def duration(delta: pd.Timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def table_height(rows: int) -> int:
    """Высота таблицы по числу строк, чтобы она читалась без внутренней прокрутки (но не выше 700 px)."""
    return min(ROW_HEIGHT * (rows + 1) + 3, 700)


def with_money(table: pd.DataFrame):
    """Таблица, где суммы показаны с разрядами: «178 000 ₽». Сортируется по-прежнему по числам."""
    return table.style.format({"amount": money})


def style_page():
    """Оформление поверх темы Streamlit: фон, карточки, вкладки, подписи. Цвета - из theme.py."""
    colors = {"bg": theme.BG, "surface": theme.SURFACE, "text": theme.TEXT, "text-2": theme.TEXT_2,
              "text-3": theme.TEXT_3, "accent": theme.ACCENT, "hairline": theme.HAIRLINE,
              "hairline-strong": theme.HAIRLINE_STRONG}
    variables = " ".join(f"--{name}: {value};" for name, value in colors.items())
    st.html(f"<style>:root {{ {variables} }}\n{CSS.read_text(encoding='utf-8')}</style>")


def show_summary(tx: pd.DataFrame, suspicious: pd.DataFrame, result):
    n_cards = len((set(tx["sender_card"]) | set(tx["receiver_card"])) - {ATM, EXTERNAL})
    columns = st.columns(6 if result else 3)
    columns[0].metric("Операций", f"{len(tx):,}".replace(",", " "), border=True)
    columns[1].metric("Карт", f"{n_cards:,}".replace(",", " "), border=True)
    columns[2].metric("Подозрительных", len(suspicious), border=True)
    if result:
        columns[3].metric("Precision", f"{result.precision:.1%}", border=True,
                          help="Какая доля отмеченных карт - действительно дропы")
        columns[4].metric("Recall", f"{result.recall:.1%}", border=True, help="Какую долю дропов нашли")
        columns[5].metric("F1", f"{result.f1:.1%}", border=True, help="Среднее гармоническое precision и recall")


def show_mitigated(events: pd.DataFrame, suspicious: pd.DataFrame, min_risk: int):
    """Карты, у которых правило сработало, но риск снижен смягчающими обстоятельствами."""
    softened = events[(events["score"] < min_risk) & ~events["card"].isin(suspicious["card"])]
    if softened.empty:
        return
    with st.expander(f"Риск снижен смягчающими обстоятельствами: {softened['card'].nunique()} карт"):
        st.caption("Правило сработало, но обстоятельства говорят в пользу владельца. В список выше эти карты "
                   "не попали; при сомнениях аналитик может проверить их вручную.")
        st.dataframe(softened.assign(rule=softened["rule"].map(RULE_TITLES))[["card", "rule", "reason", "mitigation"]],
                     hide_index=True, column_config={
                         "card": st.column_config.TextColumn("Карта", width=165),
                         "rule": st.column_config.TextColumn("Правило", width=165),
                         "reason": st.column_config.TextColumn("Что заметило правило", width="large"),
                         "mitigation": st.column_config.TextColumn("Что смягчает", width="large"),
                     })


def show_lookalikes(result):
    """Честные люди из сложной синтетики: кого из них правила приняли за дропов."""
    if result is None or result.by_situation.empty:
        return
    table = (result.by_situation.assign(title=result.by_situation["situation"].map(SITUATION_TITLES))
                                .sort_values("flagged", ascending=False, kind="stable"))
    st.subheader("Честные люди, похожие на дропов")
    st.caption("Обычные люди в ситуациях, которые со стороны похожи на схемы дропов. "
               "Всё, что попало в список подозрительных, - ложные тревоги.")
    st.dataframe(table[["title", "people", "flagged"]], hide_index=True,
                 row_height=ROW_HEIGHT, height=table_height(len(table)), column_config={
                     "title": st.column_config.TextColumn("Ситуация", width="large"),
                     "people": st.column_config.NumberColumn("Людей", width="small"),
                     "flagged": st.column_config.NumberColumn("Приняты за дропов", width="medium"),
                 })


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
    st.dataframe(table, hide_index=True, row_height=ROW_HEIGHT, height=table_height(len(table)), column_config={
        "card": st.column_config.TextColumn("Карта", width=165),
        "risk": st.column_config.ProgressColumn("Риск", min_value=0, max_value=100, format="%d", width=100,
                                                color=theme.RISK),
        "rules": st.column_config.TextColumn("Правила", width=165),
        "reason": st.column_config.TextColumn("Главная причина", width=290),
        "first_seen": st.column_config.DatetimeColumn("Впервые", format="DD.MM HH:mm", width=95),
        "events": st.column_config.NumberColumn("Сработало", width=85, help="Сколько раз сработали правила"),
        "drop": st.column_config.CheckboxColumn("Дроп", width=60, help="Дроп по разметке"),
    })
    st.download_button("Скачать список, CSV", suspicious.to_csv(index=False).encode("utf-8-sig"),
                       file_name="suspicious.csv", mime="text/csv")

    if result is None:
        return
    st.subheader("Полнота по схемам")
    st.dataframe(result.by_scheme.assign(scheme=scheme_titles(result.by_scheme["scheme"])),
                 hide_index=True, column_config={
                     "scheme": "Схема",
                     "drops": "Дроп-карт",
                     "caught": "Найдено",
                     "recall": st.column_config.NumberColumn("Recall", format="percent"),
                 })
    if not result.missed.empty:
        st.subheader("Пропущенные дроп-карты")
        st.dataframe(result.missed.assign(scheme=scheme_titles(result.missed["scheme"])), hide_index=True,
                     row_height=ROW_HEIGHT, height=table_height(len(result.missed)), column_config={
                         "card": st.column_config.TextColumn("Карта", width="medium"),
                         "scheme": "Схема",
                         "scheme_id": "Экземпляр схемы",
                         "role": "Роль",
                     })


def show_card(tx: pd.DataFrame, events: pd.DataFrame, suspicious: pd.DataFrame, drops: set):
    if suspicious.empty:
        st.info("Подозрительных карт нет, разбирать нечего.")
        return

    risk = dict(zip(suspicious["card"], suspicious["risk"], strict=True))
    card = st.selectbox("Карта", suspicious["card"], format_func=lambda c: f"{c}  (риск {risk[c]})")

    card_events = (events[events["card"] == card]
                   .sort_values(["score", "time"], ascending=[False, True])
                   .assign(rule=lambda e: e["rule"].map(RULE_TITLES)))
    st.dataframe(card_events[["rule", "score", "time", "reason", "mitigation"]], hide_index=True, column_config={
        "rule": st.column_config.TextColumn("Правило", width="small"),
        "score": st.column_config.NumberColumn("Оценка", width="small"),
        "time": st.column_config.DatetimeColumn("Когда", format="DD.MM.YYYY HH:mm", width="small"),
        "reason": st.column_config.TextColumn("Причина", width="large"),
        "mitigation": st.column_config.TextColumn("Смягчает", width="medium"),
    })

    graph_column, ops_column = st.columns([3, 2])
    with graph_column:
        st.caption("Движение денег вокруг карты: прямые связи и дальше по цепочке через подозрительные карты")
        graph = build_graph(tx, [card], risk, drops, depth=3)
        st.iframe(to_html(graph, height=480, detailed=True), height=480)
    with ops_column:
        st.caption("Операции карты")
        ops = tx[(tx["sender_card"] == card) | (tx["receiver_card"] == card)]
        incoming = ops["receiver_card"] == card
        counterparty = ops["sender_card"].where(incoming, ops["receiver_card"])
        ops = ops.assign(direction=incoming.map({True: "приход", False: "расход"}),
                         counterparty=counterparty.replace(PARTY_TITLES),
                         op_type=ops["op_type"].map(OP_TITLES).fillna(ops["op_type"]))
        st.dataframe(with_money(ops[["datetime", "direction", "amount", "counterparty", "op_type"]]),
                     hide_index=True, column_config={
                         "datetime": st.column_config.DatetimeColumn("Когда", format="DD.MM HH:mm"),
                         "direction": st.column_config.TextColumn("", width="small"),
                         "amount": st.column_config.TextColumn("Сумма", width="small"),
                         "counterparty": st.column_config.TextColumn("Контрагент", width="medium"),
                         "op_type": st.column_config.TextColumn("Тип", width="small"),
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
    st.iframe(to_html(graph, height=620), height=620)


def show_tracing(tx: pd.DataFrame, suspicious: pd.DataFrame):
    if suspicious.empty:
        st.info("Подозрительных карт нет, прослеживать нечего.")
        return

    # Начало прослеживания - деньги, пришедшие на подозрительную карту извне цепочки:
    # перевод жертвы или пополнение. Сначала - на самые рискованные карты.
    risk = dict(zip(suspicious["card"], suspicious["risk"], strict=True))
    starts = tx[tx["receiver_card"].isin(risk) & ~tx["sender_card"].isin(risk)
                & tx["op_type"].isin(["transfer", "top_up"])]
    if starts.empty:
        st.info("На подозрительные карты не приходило переводов и пополнений извне.")
        return
    starts = (starts.assign(risk=starts["receiver_card"].map(risk))
                    .sort_values(["risk", "amount"], ascending=False))
    labels = {idx: f"{op.datetime:%d.%m %H:%M}   "
                   f"{'Пополнение' if op.sender_card == EXTERNAL else op.sender_card} -> {op.receiver_card},   "
                   f"{money(op.amount)}" for idx, op in starts.iterrows()}
    start = st.selectbox("Откуда начать: поступление на подозрительную карту", list(labels), format_func=labels.get)

    with st.expander("Настройки прослеживания"):
        horizon = st.number_input("Смотреть исходящие операции карты в течение, часов", 1, 720, 72)
        max_steps = st.number_input("Шагов не больше", 1, 20, 6)
    result = trace(tx, start, pd.Timedelta(hours=horizon), max_steps)

    columns = st.columns(5)
    columns[0].metric("Прослежено", money(result.amount))
    columns[1].metric("Снято наличными", money(result.cashed), f"{result.cashed / result.amount:.0%}",
                      delta_color="off", delta_arrow="off")
    columns[2].metric("Осталось на картах", money(result.settled_total))
    columns[3].metric("Карт в цепочке", result.cards)
    columns[4].metric("До первого снятия", duration(result.time_to_cash) if result.time_to_cash is not None else "-")

    st.plotly_chart(to_sankey(result, set(risk)))
    st.caption("Допущение, как в расследованиях: первыми с карты уходят именно прослеживаемые деньги, "
               "но не больше, чем пришло. Ширина полосы - сумма.")
    flows = result.flows.assign(sender=result.flows["sender"].replace(PARTY_TITLES),
                                receiver=result.flows["receiver"].replace(PARTY_TITLES),
                                op_type=result.flows["op_type"].map(OP_TITLES).fillna(result.flows["op_type"]))
    st.dataframe(with_money(flows), hide_index=True, column_config={
        "step": st.column_config.NumberColumn("Шаг", width="small"),
        "time": st.column_config.DatetimeColumn("Когда", format="DD.MM HH:mm", width="small"),
        "sender": "Откуда",
        "receiver": "Куда",
        "amount": "Сумма",
        "op_type": "Тип",
    })


def show_about():
    st.markdown(f"""
Детектор ищет **дроп-карты**: карты, через которые прогоняют и обналичивают похищенные деньги.
Правила объяснимые: у каждой найденной карты есть причина и операции-доказательства.

| Правило | Когда срабатывает |
|---|---|
| Транзит | Пришёл перевод от 10 000 ₽, и за 6 часов с карты ушло 90-100% этой суммы |
| Воронка | За 48 часов переводы от 8+ разных карт, собрано от 50 000 ₽, и за следующие сутки ушло 80%+ |
| Быстрое обналичивание | Пришло от 30 000 ₽, и за час снято наличными 80%+ |

«Ушло» - это перевод другому человеку или снятие наличных. Оплата ипотеки, клиники, магазина -
трата своих денег, а не транзит. Транзитные карты, передающие деньги друг другу, склеиваются в цепочки.
Пороги можно менять в боковой панели и сразу видеть, как меняются результат и точность.

**Смягчающие обстоятельства** снижают оценку срабатывания, и карта не попадает в список:

- отправитель знаком с картой - операции между ними были и раньше (так банки отличают перевод
  знакомому от перевода незнакомцу; знакомство с похожей на транзитную картой не в счёт);
- доход пришёл от организации, которая платит многим (работодатель, Соцфонд), - это зарплата или пенсия;
- в воронке больше половины денег прислали знакомые.

Такие срабатывания видны под списком: решение остаётся за аналитиком.

В синтетических данных спрятаны три схемы, а среди обычных клиентов есть **ловушки**:
складчина (много мелких переводов на одну карту) и снятие наличных сразу после зарплаты.

**Сложная синтетика.** В простом режиме схемы укладываются в пороги правил, поэтому recall там 100%.
В сложном добавлены схемы, которые эти пороги обходят, как это делают настоящие дропы:

| Схема | Как обходит правила |
|---|---|
| Разветвление | Распределитель выжидает 7-24 часа и раздаёт деньги 3-4 дропам, те снимают с дневным лимитом |
| Прогрев | Недели обычной жизни перед схемой, потом крупная сумма снимается частями за сутки |
| P2P-дроп | Покупатели платят по одному раз в 7-16 часов, за 48 часов меньше 8 отправителей |

А ещё **честные люди в ситуациях, похожих на дропов**: ипотека с перевода супруга, вся зарплата
или пенсия наличными, перевод вклада между своими банками, продажа машины, ремонт на деньги
родителей, продажи на Авито, таксист с оплатой переводом, свадьба, сбор на лечение, совместная
покупка, распродажа перед переездом, помощь друзей после потери работы. Под списком видно,
кого из них правила всё равно принимают за дропов.

Код, тесты и описание: [{REPO_URL}]({REPO_URL})
""")


# --- Страница ----------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Детектор дропов", layout="wide")
    style_page()
    st.title("Детектор дропов")
    st.caption("Прототип антифрод-инструмента: поиск дроп-карт по истории переводов")

    transactions, truth, lookalikes, warnings = data_sidebar()
    rules = rules_sidebar()
    if warnings:
        st.warning("При загрузке часть строк исправлена или отброшена:")
        st.text("\n".join(warnings))

    events, suspicious = run_detection(transactions, rules)
    result = evaluate(suspicious, truth, lookalikes) if truth is not None else None
    drops = set(truth["card"]) if truth is not None else set()

    show_summary(transactions, suspicious, result)
    tab_list, tab_card, tab_network, tab_tracing, tab_about = st.tabs(
        ["Подозрительные карты", "Разбор карты", "Граф связей", "Куда ушли деньги", "Как это работает"])
    with tab_list:
        show_table(suspicious, drops, result)
        show_mitigated(events, suspicious, rules.min_risk)
        show_lookalikes(result)
    with tab_card:
        show_card(transactions, events, suspicious, drops)
    with tab_network:
        show_network(transactions, suspicious, drops)
    with tab_tracing:
        show_tracing(transactions, suspicious)
    with tab_about:
        show_about()


main()
