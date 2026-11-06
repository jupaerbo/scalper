"""Bot de Telegram interactivo para buscar productos y crear alertas.

Ejecuta con ``python src/telegram_bot.py``; lee ``TELEGRAM_BOT_TOKEN`` (y
opcionalmente ``TELEGRAM_ALLOWED_CHAT_IDS``) del entorno o de ``.env``.
"""

import logging
import os
import warnings
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from models import Product
from notifier import TELEGRAM_MAX_LENGTH
from scraper import (
    SearchError,
    parse_price,
    search_products,
)

logger = logging.getLogger(__name__)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

WAITING_NAME, WAITING_PRICE, ASK_ALERT = range(3)

ALERT_INTERVAL_SECONDS = 30 * 60
# Una búsqueda a medias caduca: el siguiente texto vuelve a ser un producto.
CONVERSATION_TIMEOUT_SECONDS = 15 * 60
MAX_ALERTS_PER_CHAT = 5
MAX_NAME_LENGTH = 100
CALLBACK_ALERT_YES = "alert:yes"
CALLBACK_ALERT_NO = "alert:no"
ALERT_JOB_PREFIX = "alert"

Snapshot = dict[str, Product]

STOCK_STATUS = {
    True: "✅ En stock",
    None: "❔ Stock sin confirmar",
    False: "❌ Agotado",
}


@dataclass(slots=True)
class AlertData:
    """Estado de una alerta periódica."""

    name: str
    max_price: float
    snapshot: Snapshot = field(default_factory=dict)


def take_snapshot(products: list[Product]) -> Snapshot:
    """Indexa los productos por URL para compararlos en la siguiente ronda."""
    return {product.url: product for product in products}


def diff_offers(old: Snapshot, new: Snapshot) -> list[str]:
    """Describe los cambios de stock o precio entre dos búsquedas.

    Solo se consideran relevantes:

    - ofertas nuevas con stock,
    - ofertas que vuelven a tener stock,
    - cambios de precio en ofertas con stock,
    - ofertas con stock que se agotan o dejan de aparecer.

    Args:
        old: Resultado de la búsqueda anterior.
        new: Resultado de la búsqueda actual.

    Returns:
        Líneas HTML, una por cambio; vacía si no hay cambios relevantes.
    """
    changes: list[str] = []
    for url, product in sorted(new.items(), key=lambda item: item[1].price):
        before = old.get(url)
        label = _offer_label(product)
        if not product.maybe_in_stock:
            if before and before.maybe_in_stock:
                changes.append(f"❌ Agotado: {label}")
        elif before is None:
            changes.append(f"🆕 Nueva oferta: {label} — {product.price_label}")
        elif not before.maybe_in_stock:
            changes.append(f"✅ Vuelve a haber stock: {label} — {product.price_label}")
        elif before.price != product.price:
            arrow = "📉" if product.price < before.price else "📈"
            changes.append(
                f"{arrow} Precio: {label} — "
                f"{before.price_label} → {product.price_label}"
            )
    for url, before in old.items():
        if url not in new and before.maybe_in_stock:
            changes.append(f"❌ Ya no disponible: {_offer_label(before)}")
    return changes


def _offer_label(product: Product) -> str:
    url = escape(product.url, quote=True)
    return f'<a href="{url}">{escape(product.title)}</a> ({escape(product.store_name)})'


def format_results(products: list[Product], max_price: float) -> str:
    """Lista HTML de ofertas ≤ ``max_price`` con su precio y estado de stock.

    Primero las ofertas con stock, luego las de stock sin confirmar y por último
    las agotadas, cada grupo de menor a mayor precio. Se recorta a
    ``TELEGRAM_MAX_LENGTH`` con ``… y N más``.
    """
    offers = sorted(
        (p for p in products if p.price <= max_price), key=lambda p: p.sort_key
    )
    if not offers:
        return (
            f"😕 No he encontrado ofertas con precio visible por debajo de "
            f"{max_price:.2f}€.\nPrueba con otro término o un precio mayor en /buscar."
        )

    in_stock = sum(p.available is True for p in offers)
    message = (
        f"🛒 <b>{len(offers)} oferta(s) ≤ {max_price:.2f}€</b> ({in_stock} con stock)\n"
    )
    for shown, product in enumerate(offers):
        status = STOCK_STATUS[product.available]
        line = (
            f"\n• <b>{escape(product.title)}</b>\n"
            f"  💰 {product.price_label} · {status} · {escape(product.store_name)}\n"
            f'  🔗 <a href="{escape(product.url, quote=True)}">Ver oferta</a>'
        )
        tail = f"\n\n… y {len(offers) - shown} más"
        if len(message) + len(line) + len(tail) > TELEGRAM_MAX_LENGTH:
            return message + tail
        message += line
    return message


def alert_job_name(chat_id: int, name: str, max_price: float) -> str:
    """Nombre único de la tarea de alerta de un chat."""
    return f"{ALERT_JOB_PREFIX}:{chat_id}:{name.casefold()}:{max_price:.2f}"


def _alert_jobs(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list:
    if context.job_queue is None:
        return []
    prefix = f"{ALERT_JOB_PREFIX}:{chat_id}:"
    return [
        job
        for job in context.job_queue.jobs()
        if job.name and job.name.startswith(prefix) and not job.removed
    ]


# --- Conversación -------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/start`` y ``/buscar``: saluda y pide el producto."""
    context.user_data.clear()
    user = update.effective_user
    greeting = f"¡Hola, {escape(user.first_name)}!" if user else "¡Hola!"
    await update.effective_message.reply_text(
        f"{greeting} 👋\n¿Qué producto quieres buscar?\n\n"
        "Consejo: no hace falta /buscar, puedes escribirme directamente el "
        "nombre del producto (ej. <i>RTX 4070</i>).\n"
        "Puedes cancelar en cualquier momento con /cancel.",
        parse_mode=ParseMode.HTML,
    )
    return WAITING_NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Guarda el término de búsqueda y pide el precio máximo.

    Es también punto de entrada: cualquier texto que no sea un comando (en un
    chat privado) inicia una búsqueda directa sin pasar por ``/buscar``.
    """
    context.user_data.clear()
    name = (update.effective_message.text or "").strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        await update.effective_message.reply_text(
            f"Escribe un nombre de producto (máximo {MAX_NAME_LENGTH} caracteres)."
        )
        return WAITING_NAME

    context.user_data["name"] = name
    await update.effective_message.reply_text(
        f"Perfecto: <b>{escape(name)}</b>.\n"
        "¿Cuál es tu precio máximo en euros? (ej. 600)",
        parse_mode=ParseMode.HTML,
    )
    return WAITING_PRICE


async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Valida el precio, busca y muestra las ofertas con precio y stock."""
    text = (update.effective_message.text or "").strip()
    max_price = parse_price(text)
    if max_price is None or text.lstrip().startswith("-"):
        await update.effective_message.reply_text(
            "Ese precio no es válido. Escribe un número positivo, por ejemplo "
            "599,99 (o /buscar para buscar otro producto)."
        )
        return WAITING_PRICE

    name: str = context.user_data["name"]
    context.user_data["max_price"] = max_price
    await update.effective_message.reply_text(
        f"🔎 Buscando <b>{escape(name)}</b> por debajo de {max_price:.2f}€…",
        parse_mode=ParseMode.HTML,
    )
    await update.effective_chat.send_action(ChatAction.TYPING)

    try:
        products = await search_products(name, max_price)
    except SearchError as exc:
        logger.warning("Búsqueda fallida para %r: %r", name, exc)
        return await _end(
            update,
            context,
            f"⚠️ No he podido buscar en internet ahora mismo: {exc}.\n"
            "Inténtalo más tarde escribiendo el producto o con /buscar.",
        )
    except Exception:
        logger.exception("Error inesperado buscando %r", name)
        return await _end(
            update,
            context,
            "⚠️ Ha ocurrido un error inesperado al buscar. "
            "Inténtalo de nuevo escribiendo el producto o con /buscar.",
        )

    context.user_data["snapshot"] = take_snapshot(products)
    await update.effective_message.reply_text(
        format_results(products, max_price),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔔 Crear Alerta", callback_data=CALLBACK_ALERT_YES
                ),
                InlineKeyboardButton("❌ Cancelar", callback_data=CALLBACK_ALERT_NO),
            ]
        ]
    )
    minutes = ALERT_INTERVAL_SECONDS // 60
    await update.effective_message.reply_text(
        f"¿Quieres que vigile esta búsqueda cada {minutes} minutos y te avise "
        "si cambia el stock o el precio?",
        reply_markup=keyboard,
    )
    return ASK_ALERT


async def alert_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Crea (o no) la alerta periódica según el botón pulsado."""
    query = update.callback_query
    await query.answer()

    if query.data != CALLBACK_ALERT_YES:
        await query.edit_message_text("De acuerdo, no crearé ninguna alerta. 👍")
        context.user_data.clear()
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    name: str = context.user_data["name"]
    max_price: float = context.user_data["max_price"]
    job_name = alert_job_name(chat_id, name, max_price)
    existing = _alert_jobs(context, chat_id)

    if context.job_queue is None:
        text = "⚠️ Las alertas no están disponibles en este servidor."
    elif any(job.name == job_name for job in existing):
        text = "Ya tenías una alerta igual activa. 👌"
    elif len(existing) >= MAX_ALERTS_PER_CHAT:
        text = (
            f"Has llegado al máximo de {MAX_ALERTS_PER_CHAT} alertas. "
            "Usa /parar para borrarlas."
        )
    else:
        context.job_queue.run_repeating(
            check_alert,
            interval=ALERT_INTERVAL_SECONDS,
            first=ALERT_INTERVAL_SECONDS,
            chat_id=chat_id,
            name=job_name,
            data=AlertData(name, max_price, context.user_data.get("snapshot", {})),
        )
        text = (
            f"🔔 Alerta creada para <b>{escape(name)}</b> ≤ {max_price:.2f}€. "
            "Te avisaré solo si cambia el stock o el precio.\n"
            "Consulta tus alertas con /alertas y bórralas con /parar."
        )

    await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/cancel``: aborta la conversación."""
    return await _end(
        update,
        context,
        "Búsqueda cancelada. Escríbeme el nombre de un producto (o /buscar) "
        "cuando quieras empezar de nuevo.",
    )


async def _end(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> int:
    """Responde ``text``, olvida la búsqueda en curso y cierra la conversación."""
    context.user_data.clear()
    await update.effective_message.reply_text(text)
    return ConversationHandler.END


async def expired_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Botón de una búsqueda anterior que ya no está en curso."""
    query = update.callback_query
    await query.answer("Esta búsqueda ya no está activa.")
    await query.edit_message_reply_markup(reply_markup=None)


# --- Alertas ------------------------------------------------------------------


async def check_alert(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tarea periódica: repite la búsqueda y avisa solo si hay cambios."""
    job = context.job
    data: AlertData = job.data
    try:
        products = await search_products(data.name, data.max_price)
    except SearchError as exc:
        # Se reintenta en la siguiente ronda; no se molesta al usuario.
        logger.warning("Alerta %s: búsqueda fallida: %r", job.name, exc)
        return

    snapshot = take_snapshot(products)
    changes = diff_offers(data.snapshot, snapshot)
    data.snapshot = snapshot
    if not changes:
        return

    header = f"🔔 <b>Cambios en «{escape(data.name)}»</b> (≤ {data.max_price:.2f}€)\n\n"
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=header + "\n".join(changes),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/alertas``: muestra las alertas activas del chat."""
    jobs = _alert_jobs(context, update.effective_chat.id)
    if not jobs:
        await update.effective_message.reply_text("No tienes alertas activas.")
        return
    lines = [
        f"• <b>{escape(job.data.name)}</b> ≤ {job.data.max_price:.2f}€" for job in jobs
    ]
    await update.effective_message.reply_text(
        "🔔 Alertas activas:\n" + "\n".join(lines), parse_mode=ParseMode.HTML
    )


async def stop_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/parar``: elimina todas las alertas del chat."""
    jobs = _alert_jobs(context, update.effective_chat.id)
    for job in jobs:
        job.schedule_removal()
    await update.effective_message.reply_text(
        f"🗑️ {len(jobs)} alerta(s) eliminada(s)." if jobs else "No tenías alertas."
    )


# --- Aplicación ---------------------------------------------------------------


def parse_allowed_chats(raw: str | None) -> set[int]:
    """Convierte ``"1, -100200"`` en ``{1, -100200}``; vacío = sin restricción."""
    return {int(part) for part in (raw or "").replace(" ", "").split(",") if part}


def build_application(
    token: str, allowed_chat_ids: set[int] | None = None
) -> Application:
    """Crea la aplicación del bot con todos sus handlers.

    Args:
        token: Token del bot.
        allowed_chat_ids: Si se indica, el bot solo responde a esos chats.
    """
    chats = filters.Chat(chat_id=allowed_chat_ids) if allowed_chat_ids else filters.ALL
    application = Application.builder().token(token).build()
    application.add_handler(_conversation(chats))
    application.add_handler(CommandHandler("alertas", list_alerts, filters=chats))
    application.add_handler(CommandHandler("parar", stop_alerts, filters=chats))
    application.add_handler(CallbackQueryHandler(expired_button, pattern=r"^alert:"))
    application.add_error_handler(on_error)
    return application


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra cualquier excepción no controlada sin detener el bot."""
    logger.error("Error no controlado en el bot", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Algo ha fallado. Inténtalo de nuevo con /buscar."
        )


def _conversation(chats: filters.BaseFilter) -> ConversationHandler:
    """Conversación de búsqueda.

    - Se entra con ``/start``, ``/buscar`` o escribiendo directamente el nombre
      de un producto en un chat privado (en grupos se exige ``/buscar`` para no
      reaccionar a cada mensaje).
    - Sin ``allow_reentry``: así el texto con el precio no se toma como un
      producto nuevo. ``/buscar`` reinicia desde cualquier estado (fallback).
    - Tras los resultados (``ASK_ALERT``) un texto nuevo inicia otra búsqueda.
    """
    text = filters.TEXT & ~filters.COMMAND & chats
    direct_search = MessageHandler(text & filters.ChatType.PRIVATE, receive_name)
    restart = CommandHandler(["start", "buscar"], start, filters=chats)
    # El CallbackQueryHandler solo responde al último mensaje con botones, así
    # que el aviso de PTB sobre per_message=False no aplica.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", r".*per_message=False", PTBUserWarning)
        return ConversationHandler(
            entry_points=[restart, direct_search],
            states={
                WAITING_NAME: [MessageHandler(text, receive_name)],
                WAITING_PRICE: [MessageHandler(text, receive_price)],
                ASK_ALERT: [
                    CallbackQueryHandler(alert_choice, pattern=r"^alert:"),
                    MessageHandler(text, receive_name),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel, filters=chats), restart],
            conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        )


def main() -> None:
    """Arranca el bot en modo *polling*."""
    load_dotenv(ENV_FILE)
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    # httpx registra cada URL, que incluye el token del bot.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Define TELEGRAM_BOT_TOKEN en el entorno o en .env")
    allowed = parse_allowed_chats(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS"))
    build_application(token, allowed).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
