import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from telegram import Chat, InlineKeyboardMarkup, Message, MessageEntity, Update, User
from telegram.ext import CallbackQueryHandler, ConversationHandler, filters

import telegram_bot as bot
from models import Product
from scraper import SearchError

CHAT_ID = 42

A_OK = Product("RTX A", 550.0, True, "Tienda A", "https://a.test/1")
B_OUT = Product("RTX B", 500.0, False, "Tienda B", "https://b.test/2")
C_OK = Product("RTX <C> & Co", 590.0, True, "Tienda C", "https://c.test/3")


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


def make_update(text: str | None = None, callback_data: str | None = None) -> Any:
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    query = None
    if callback_data is not None:
        query = SimpleNamespace(
            data=callback_data, answer=AsyncMock(), edit_message_text=AsyncMock()
        )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=CHAT_ID, send_action=AsyncMock()),
        effective_user=SimpleNamespace(first_name="Ana"),
        callback_query=query,
    )


class FakeJob:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


class FakeJobQueue:
    def __init__(self) -> None:
        self._jobs: list[FakeJob] = []

    def run_repeating(self, callback: Any, **kwargs: Any) -> FakeJob:
        job = FakeJob(callback=callback, **kwargs)
        self._jobs.append(job)
        return job

    def jobs(self) -> list[FakeJob]:
        return list(self._jobs)


def make_context(user_data: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        user_data={} if user_data is None else user_data,
        job_queue=FakeJobQueue(),
        bot=SimpleNamespace(send_message=AsyncMock()),
        job=None,
    )


def replies(update: Any) -> list[str]:
    return [call.args[0] for call in update.effective_message.reply_text.call_args_list]


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []
    results: list[Product] = [A_OK, B_OUT, C_OK]

    async def fake(name: str, max_price: float) -> list[Product]:
        calls.append((name, max_price))
        return [p for p in results if p.price <= max_price]

    monkeypatch.setattr(bot, "search_products", fake)
    return calls


# --- Conversación -------------------------------------------------------------


def test_start_greets_and_asks_for_name() -> None:
    update, context = make_update(), make_context({"name": "viejo"})

    state = run(bot.start(update, context))

    assert state == bot.WAITING_NAME
    assert context.user_data == {}
    assert "¡Hola, Ana!" in replies(update)[0]
    assert "producto" in replies(update)[0]


def test_receive_name_stores_term_and_asks_for_price() -> None:
    update, context = make_update("  rtx 4070  "), make_context()

    state = run(bot.receive_name(update, context))

    assert state == bot.WAITING_PRICE
    assert context.user_data["name"] == "rtx 4070"
    assert "precio máximo" in replies(update)[0]


@pytest.mark.parametrize("text", ["", "   ", "x" * 101])
def test_receive_name_rejects_invalid(text: str) -> None:
    update, context = make_update(text), make_context()

    assert run(bot.receive_name(update, context)) == bot.WAITING_NAME
    assert "name" not in context.user_data


@pytest.mark.parametrize("text", ["barato", "-5", ""])
def test_receive_price_rejects_invalid(
    text: str, search: list[tuple[str, float]]
) -> None:
    update, context = make_update(text), make_context({"name": "rtx"})

    assert run(bot.receive_price(update, context)) == bot.WAITING_PRICE
    assert search == []
    assert "no es válido" in replies(update)[0]


def test_receive_price_searches_and_lists_price_and_stock(
    search: list[tuple[str, float]],
) -> None:
    update, context = make_update("600,00 €"), make_context({"name": "rtx"})

    state = run(bot.receive_price(update, context))

    assert state == bot.ASK_ALERT
    assert search == [("rtx", 600.0)]
    searching, results, question = replies(update)
    assert "Buscando" in searching
    assert "3 oferta(s) ≤ 600.00€</b> (2 con stock)" in results
    assert "• <b>RTX A</b>\n  💰 550.00€ · ✅ En stock · Tienda A" in results
    assert "• <b>RTX &lt;C&gt; &amp; Co</b>\n  💰 590.00€ · ✅ En stock" in results
    assert "• <b>RTX B</b>\n  💰 500.00€ · ❌ Agotado · Tienda B" in results
    assert '<a href="https://a.test/1">Ver oferta</a>' in results
    # Primero con stock (por precio) y después agotados.
    assert results.index("RTX A") < results.index("RTX &lt;C&gt;")
    assert results.index("RTX &lt;C&gt;") < results.index("RTX B")
    keyboard = update.effective_message.reply_text.call_args.kwargs["reply_markup"]
    assert isinstance(keyboard, InlineKeyboardMarkup)
    buttons = [(b.text, b.callback_data) for b in keyboard.inline_keyboard[0]]
    assert buttons == [
        ("🔔 Crear Alerta", bot.CALLBACK_ALERT_YES),
        ("❌ Cancelar", bot.CALLBACK_ALERT_NO),
    ]
    assert "30 minutos" in question
    assert set(context.user_data["snapshot"]) == {A_OK.url, B_OUT.url, C_OK.url}


def test_receive_price_without_stock_still_offers_alert(
    search: list[tuple[str, float]],
) -> None:
    update, context = make_update("520"), make_context({"name": "rtx"})

    assert run(bot.receive_price(update, context)) == bot.ASK_ALERT
    assert "(0 con stock)" in replies(update)[1]
    assert "❌ Agotado" in replies(update)[1]


def test_receive_price_without_offers(search: list[tuple[str, float]]) -> None:
    update, context = make_update("100"), make_context({"name": "rtx"})

    assert run(bot.receive_price(update, context)) == bot.ASK_ALERT
    assert "No he encontrado ofertas" in replies(update)[1]


def test_format_results_is_truncated_to_telegram_limit() -> None:
    many = [
        Product("x" * 120, float(i), i % 2 == 0, "T", f"https://t.test/{i}")
        for i in range(200)
    ]

    text = bot.format_results(many, 1000.0)

    assert len(text) <= bot.TELEGRAM_MAX_LENGTH
    shown = text.count("Ver oferta")
    assert text.endswith(f"… y {200 - shown} más")


def test_receive_price_ends_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bot,
        "search_products",
        AsyncMock(side_effect=SearchError("no hay conexión a internet")),
    )
    update, context = make_update("600"), make_context({"name": "rtx"})

    assert run(bot.receive_price(update, context)) == ConversationHandler.END
    assert "No he podido buscar en internet" in replies(update)[-1]
    assert context.user_data == {}


def _choose(data: str, context: Any) -> tuple[int, Any]:
    update = make_update(callback_data=data)
    return run(bot.alert_choice(update, context)), update


def _alert_context() -> Any:
    snapshot = bot.take_snapshot([A_OK])
    return make_context({"name": "rtx", "max_price": 600.0, "snapshot": snapshot})


def test_alert_yes_schedules_repeating_job() -> None:
    context = _alert_context()

    state, update = _choose(bot.CALLBACK_ALERT_YES, context)

    assert state == ConversationHandler.END
    update.callback_query.answer.assert_awaited_once()
    (job,) = context.job_queue.jobs()
    assert job.callback is bot.check_alert
    assert job.interval == 30 * 60
    assert job.first == 30 * 60
    assert job.chat_id == CHAT_ID
    assert job.name == bot.alert_job_name(CHAT_ID, "rtx", 600.0)
    assert job.data == bot.AlertData("rtx", 600.0, {A_OK.url: A_OK})
    text = update.callback_query.edit_message_text.call_args.args[0]
    assert "Alerta creada" in text
    assert context.user_data == {}


def test_alert_yes_does_not_duplicate_same_alert() -> None:
    context = _alert_context()
    _choose(bot.CALLBACK_ALERT_YES, context)
    context.user_data.update(name="RTX", max_price=600.0)

    _, update = _choose(bot.CALLBACK_ALERT_YES, context)

    assert len(context.job_queue.jobs()) == 1
    assert "Ya tenías" in update.callback_query.edit_message_text.call_args.args[0]


def test_alert_yes_respects_limit_per_chat() -> None:
    context = _alert_context()
    for i in range(bot.MAX_ALERTS_PER_CHAT):
        context.user_data.update(name=f"p{i}", max_price=10.0)
        _choose(bot.CALLBACK_ALERT_YES, context)
    context.user_data.update(name="otra", max_price=10.0)

    _, update = _choose(bot.CALLBACK_ALERT_YES, context)

    assert len(context.job_queue.jobs()) == bot.MAX_ALERTS_PER_CHAT
    assert "máximo" in update.callback_query.edit_message_text.call_args.args[0]


def test_alert_no_ends_without_job() -> None:
    context = _alert_context()

    state, update = _choose(bot.CALLBACK_ALERT_NO, context)

    assert state == ConversationHandler.END
    assert context.job_queue.jobs() == []
    assert "no crearé" in update.callback_query.edit_message_text.call_args.args[0]


def test_cancel_ends_conversation() -> None:
    update, context = make_update("/cancel"), make_context({"name": "rtx"})

    assert run(bot.cancel(update, context)) == ConversationHandler.END
    assert context.user_data == {}
    assert "cancelada" in replies(update)[0]


# --- Alertas periódicas ---------------------------------------------------------


def _offer(url: str, price: float, available: bool = True) -> Product:
    return Product(f"P{url}", price, available, "T", f"https://t.test/{url}")


def test_diff_offers_reports_only_stock_and_price_changes() -> None:
    old = bot.take_snapshot(
        [
            _offer("same", 10.0),
            _offer("cheaper", 20.0),
            _offer("pricier", 30.0),
            _offer("restock", 40.0, available=False),
            _offer("soldout", 50.0),
            _offer("gone", 60.0),
            _offer("out-still", 70.0, available=False),
        ]
    )
    new = bot.take_snapshot(
        [
            _offer("same", 10.0),
            _offer("cheaper", 15.0),
            _offer("pricier", 35.0),
            _offer("restock", 40.0),
            _offer("soldout", 50.0, available=False),
            _offer("out-still", 65.0, available=False),
            _offer("new", 5.0),
            _offer("new-out", 6.0, available=False),
        ]
    )

    changes = bot.diff_offers(old, new)

    assert len(changes) == 6
    joined = "\n".join(changes)
    assert "🆕 Nueva oferta" in joined and "Pnew<" in joined
    assert "📉 Precio" in joined and "20.00€ → 15.00€" in joined
    assert "📈 Precio" in joined and "30.00€ → 35.00€" in joined
    assert "✅ Vuelve a haber stock" in joined and "Prestock" in joined
    assert "❌ Agotado" in joined and "Psoldout" in joined
    assert "❌ Ya no disponible" in joined and "Pgone" in joined
    for unchanged in ("Psame", "Pout-still", "Pnew-out"):
        assert unchanged not in joined


def test_diff_offers_empty_when_nothing_changes() -> None:
    snapshot = bot.take_snapshot([A_OK, B_OUT])

    assert bot.diff_offers(snapshot, dict(snapshot)) == []


def _job_context(data: bot.AlertData) -> Any:
    context = make_context()
    context.job = SimpleNamespace(data=data, chat_id=CHAT_ID, name="alert:42:rtx")
    return context


def test_check_alert_notifies_changes_and_updates_snapshot(
    search: list[tuple[str, float]],
) -> None:
    data = bot.AlertData("rtx", 600.0, bot.take_snapshot([A_OK]))
    context = _job_context(data)

    run(bot.check_alert(context))

    assert search == [("rtx", 600.0)]
    context.bot.send_message.assert_awaited_once()
    kwargs = context.bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == CHAT_ID
    assert "Cambios en «rtx»" in kwargs["text"]
    assert "Nueva oferta" in kwargs["text"]
    assert set(data.snapshot) == {A_OK.url, B_OUT.url, C_OK.url}


def test_check_alert_is_silent_without_changes(
    search: list[tuple[str, float]],
) -> None:
    data = bot.AlertData("rtx", 600.0, bot.take_snapshot([A_OK, B_OUT, C_OK]))
    context = _job_context(data)

    run(bot.check_alert(context))

    context.bot.send_message.assert_not_awaited()


def test_check_alert_survives_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bot,
        "search_products",
        AsyncMock(side_effect=SearchError("no hay conexión a internet")),
    )
    data = bot.AlertData("rtx", 600.0, bot.take_snapshot([A_OK]))
    context = _job_context(data)

    run(bot.check_alert(context))

    context.bot.send_message.assert_not_awaited()
    assert data.snapshot == {A_OK.url: A_OK}


def test_list_and_stop_alerts() -> None:
    context = _alert_context()
    _choose(bot.CALLBACK_ALERT_YES, context)
    update = make_update("/alertas")

    run(bot.list_alerts(update, context))
    run(bot.stop_alerts(update, context))
    run(bot.list_alerts(update, context))

    listed, stopped, empty = replies(update)
    assert "<b>rtx</b> ≤ 600.00€" in listed
    assert "1 alerta(s) eliminada(s)" in stopped
    assert "No tienes alertas" in empty


# --- Aplicación -----------------------------------------------------------------


def test_build_application_wires_conversation() -> None:
    app = bot.build_application("123456:TEST-token", {CHAT_ID})

    conversation = next(
        h for h in app.handlers[0] if isinstance(h, ConversationHandler)
    )
    commands = [e for e in conversation.entry_points if hasattr(e, "commands")]
    assert {c for e in commands for c in e.commands} == {"start", "buscar"}
    assert any(e.callback is bot.receive_name for e in conversation.entry_points)
    assert set(conversation.states) == {
        bot.WAITING_NAME,
        bot.WAITING_PRICE,
        bot.ASK_ALERT,
    }
    assert isinstance(conversation.states[bot.ASK_ALERT][0], CallbackQueryHandler)
    assert {c for f in conversation.fallbacks for c in f.commands} == {
        "cancel",
        "start",
        "buscar",
    }
    assert app.job_queue is not None


# --- Búsqueda directa sin /buscar -------------------------------------------------

USER_ID = 7


def _tg_message_update(text: str, chat_type: str = Chat.PRIVATE) -> Update:
    entities = []
    if text.startswith("/"):
        command = text.split()[0]
        entities = [MessageEntity(MessageEntity.BOT_COMMAND, 0, len(command))]
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(CHAT_ID, chat_type),
        from_user=User(USER_ID, "Ana", is_bot=False),
        text=text,
        entities=entities,
    )
    message.set_bot(SimpleNamespace(username="precios_bot"))  # type: ignore[arg-type]
    return Update(update_id=1, message=message)


def _routed(text: str, state: int | None = None, chat_type: str = Chat.PRIVATE) -> Any:
    """Callback al que la conversación enviaría ``text`` en el estado dado."""
    conversation = bot._conversation(filters.ALL)
    if state is not None:
        conversation._conversations[(CHAT_ID, USER_ID)] = state
    result = conversation.check_update(_tg_message_update(text, chat_type))
    return None if result is None else result[2].callback


def test_plain_text_starts_a_direct_search() -> None:
    assert _routed("RTX 4070") is bot.receive_name


def test_buscar_command_still_works() -> None:
    assert _routed("/buscar") is bot.start
    assert _routed("/start") is bot.start


def test_direct_search_only_in_private_chats() -> None:
    assert _routed("hola a todos", chat_type=Chat.GROUP) is None
    assert _routed("/buscar", chat_type=Chat.GROUP) is bot.start


def test_price_message_is_not_taken_as_a_new_product() -> None:
    assert _routed("600", state=bot.WAITING_PRICE) is bot.receive_price
    assert _routed("RTX 4070", state=bot.WAITING_NAME) is bot.receive_name


def test_new_text_after_results_starts_another_search() -> None:
    assert _routed("RTX 3060", state=bot.ASK_ALERT) is bot.receive_name


def test_buscar_and_cancel_work_from_any_state() -> None:
    assert _routed("/buscar", state=bot.WAITING_PRICE) is bot.start
    assert _routed("/cancel", state=bot.WAITING_PRICE) is bot.cancel


def test_direct_search_flow_asks_only_for_price(
    search: list[tuple[str, float]],
) -> None:
    context = make_context({"name": "anterior", "snapshot": {"x": A_OK}})
    first = make_update("RTX 4070")

    assert run(bot.receive_name(first, context)) == bot.WAITING_PRICE
    assert context.user_data == {"name": "RTX 4070"}
    assert "precio máximo" in replies(first)[0]

    second = make_update("600")
    assert run(bot.receive_price(second, context)) == bot.ASK_ALERT
    assert search == [("RTX 4070", 600.0)]


def test_expired_button_is_answered() -> None:
    update = make_update(callback_data=bot.CALLBACK_ALERT_YES)
    update.callback_query.edit_message_reply_markup = AsyncMock()

    run(bot.expired_button(update, make_context()))

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(
        reply_markup=None
    )


def test_parse_allowed_chats() -> None:
    assert bot.parse_allowed_chats("1, -100200,") == {1, -100200}
    assert bot.parse_allowed_chats(None) == set()


def test_receive_price_reports_dns_failure_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = SearchError("no se pudo resolver el dominio (fallo de DNS o sin internet)")
    monkeypatch.setattr(bot, "search_products", AsyncMock(side_effect=error))
    update, context = make_update("600"), make_context({"name": "rtx"})

    assert run(bot.receive_price(update, context)) == ConversationHandler.END
    message = replies(update)[-1]
    assert "No he podido buscar en internet" in message
    assert "DNS" in message
    assert "/buscar" in message


def test_receive_price_survives_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bot, "search_products", AsyncMock(side_effect=RuntimeError("boom"))
    )
    update, context = make_update("600"), make_context({"name": "rtx"})

    assert run(bot.receive_price(update, context)) == ConversationHandler.END
    assert "error inesperado" in replies(update)[-1]
    assert context.user_data == {}


def test_error_handler_replies_without_raising() -> None:
    from telegram import Update

    update = MagicMock(spec=Update)
    update.effective_message.reply_text = AsyncMock()
    context = SimpleNamespace(error=RuntimeError("boom"))

    run(bot.on_error(update, context))

    update.effective_message.reply_text.assert_awaited_once()


# --- Servidor HTTP para Render ----------------------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 10000),
        ({"PORT": "8080"}, 8080),
        ({"PORT": " 3000 "}, 3000),
        ({"PORT": ""}, 10000),
        ({"PORT": "abc"}, 10000),
        ({"PORT": "70000"}, 10000),
    ],
)
def test_health_port_reads_env(env: dict[str, str], expected: int) -> None:
    assert bot.health_port(env) == expected


def _base_url(runner: Any) -> str:
    host, port = runner.addresses[0][:2]
    return f"http://{host}:{port}"


def test_health_server_answers_200_ok_on_root() -> None:
    async def scenario() -> list[tuple[int, str]]:
        runner = await bot.start_health_server(0, host="127.0.0.1")
        try:
            async with httpx.AsyncClient(base_url=_base_url(runner)) as client:
                get, head, other = (
                    await client.get("/"),
                    await client.head("/"),
                    await client.get("/otra"),
                )
        finally:
            await runner.cleanup()
        return [(r.status_code, r.text) for r in (get, head, other)]

    get, head, other = run(scenario())

    assert get == (200, "OK")
    assert head[0] == 200
    assert other[0] == 404


def test_build_application_without_port_has_no_http_server() -> None:
    app = bot.build_application("123456:TEST-token")

    assert app.post_init is None
    assert app.post_shutdown is None


def test_http_server_follows_application_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_init lo arranca y post_shutdown lo cierra, sin tocar los handlers."""
    started: list[Any] = []
    real_start = bot.start_health_server

    async def local_start(port: int) -> Any:
        runner = await real_start(port, host="127.0.0.1")
        started.append(runner)
        return runner

    monkeypatch.setattr(bot, "start_health_server", local_start)
    app = bot.build_application("123456:TEST-token", http_port=0)
    handlers_before = {group: list(hs) for group, hs in app.handlers.items()}

    async def scenario() -> tuple[int, type[Exception] | None]:
        await app.post_init(app)
        url = _base_url(started[0])
        async with httpx.AsyncClient() as client:
            status = (await client.get(url)).status_code
        await app.post_shutdown(app)
        # Cliente nuevo: sin una conexión keep-alive reutilizada, el puerto
        # cerrado da siempre ConnectError (antes, a veces, ReadError).
        async with httpx.AsyncClient() as client:
            try:
                await client.get(url)
                error = None
            except httpx.ConnectError as exc:
                error = type(exc)
        return status, error

    status, error = run(scenario())

    assert status == 200
    assert error is httpx.ConnectError  # cerrado al apagar el bot
    assert {g: list(hs) for g, hs in app.handlers.items()} == handlers_before


def test_bot_handlers_and_http_server_share_the_event_loop(
    search: list[tuple[str, float]],
) -> None:
    """El bot sigue atendiendo mensajes mientras el servidor responde."""

    async def scenario() -> tuple[int, int]:
        runner = await bot.start_health_server(0, host="127.0.0.1")
        context = make_context({"name": "rtx"})
        update = make_update("600")
        try:
            async with httpx.AsyncClient(base_url=_base_url(runner)) as client:
                state, response = await asyncio.gather(
                    bot.receive_price(update, context), client.get("/")
                )
        finally:
            await runner.cleanup()
        return state, response.status_code

    state, status = run(scenario())

    assert state == bot.ASK_ALERT
    assert status == 200
    assert search == [("rtx", 600.0)]


def test_port_in_use_fails_cleanly() -> None:
    async def scenario() -> None:
        first = await bot.start_health_server(0, host="127.0.0.1")
        try:
            port = first.addresses[0][1]
            with pytest.raises(OSError):
                await bot.start_health_server(port, host="127.0.0.1")
        finally:
            await first.cleanup()

    run(scenario())
