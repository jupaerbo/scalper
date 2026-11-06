"""Alertas de ofertas por Telegram y email."""

import asyncio
import os
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from html import escape
from typing import NotRequired, Required, TypedDict

import httpx

from models import Product

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_LENGTH = 4096
REQUEST_TIMEOUT = 10.0


class NotificationError(Exception):
    """Fallo al enviar una alerta. El mensaje nunca incluye credenciales."""


class SmtpConfig(TypedDict, total=False):
    """Configuración del servidor SMTP.

    Si no se indica ``use_ssl`` se usa STARTTLS (puerto 587 por defecto).
    """

    host: Required[str]
    port: NotRequired[int]
    username: NotRequired[str]
    password: NotRequired[str]
    from_email: NotRequired[str]
    use_ssl: NotRequired[bool]


def smtp_config_from_env(env: Mapping[str, str] = os.environ) -> SmtpConfig | None:
    """Lee la configuración SMTP de ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USER``,
    ``SMTP_PASSWORD``, ``SMTP_FROM`` y ``SMTP_SSL``.

    Returns:
        La configuración, o ``None`` si falta ``SMTP_HOST``.
    """
    host = env.get("SMTP_HOST", "").strip()
    if not host:
        return None
    config: SmtpConfig = {"host": host}
    if port := env.get("SMTP_PORT", "").strip():
        config["port"] = int(port)
    if username := env.get("SMTP_USER"):
        config["username"] = username
    if password := env.get("SMTP_PASSWORD"):
        config["password"] = password
    if from_email := env.get("SMTP_FROM"):
        config["from_email"] = from_email
    if env.get("SMTP_SSL", "").strip().lower() in {"1", "true", "yes", "si", "sí"}:
        config["use_ssl"] = True
    return config


def filter_alertable(
    products: list[Product], max_price: float | None = None
) -> list[Product]:
    """Ofertas que merecen alerta: no agotadas y, si se indica, ≤ ``max_price``.

    Incluye las de stock desconocido (``available is None``), habituales en
    resultados de búsqueda web; los mensajes las marcan como sin confirmar.

    Returns:
        Las ofertas ordenadas de menor a mayor precio.
    """
    return sorted(
        (
            p
            for p in products
            if p.maybe_in_stock and (max_price is None or p.price <= max_price)
        ),
        key=lambda p: p.price,
    )


def format_telegram_message(products: list[Product]) -> str:
    """Mensaje HTML para Telegram, recortado a ``TELEGRAM_MAX_LENGTH``.

    Si no caben todas las ofertas, se añade una línea ``… y N más``.
    """
    confirmed = all(p.available for p in products)
    header = (
        "🚨 <b>¡Oferta encontrada con stock!</b>"
        if confirmed
        else "🚨 <b>¡Oferta encontrada!</b>"
    )
    blocks = [
        (
            f"• <b>{escape(p.title)}</b>\n"
            f"  💰 Precio: {p.price_label}\n"
            f"  🏪 Tienda: {escape(p.store_name)}\n"
            + ("" if p.available else "  ❔ Stock sin confirmar\n")
            + f'  🔗 <a href="{escape(p.url, quote=True)}">Ver oferta</a>'
        )
        for p in products
    ]
    message = header
    for shown, block in enumerate(blocks):
        remaining = len(blocks) - shown - 1
        tail = f"\n\n… y {remaining} más" if remaining else ""
        candidate = f"{message}\n\n{block}"
        if len(candidate) + len(tail) > TELEGRAM_MAX_LENGTH:
            return f"{message}\n\n… y {len(blocks) - shown} más"
        message = candidate
    return message


def build_email(
    products: list[Product], from_email: str, to_email: str
) -> EmailMessage:
    """Email con versión en texto plano y en HTML."""
    message = EmailMessage()
    message["Subject"] = f"Alerta de precio: {len(products)} oferta(s) disponible(s)"
    message["From"] = from_email
    message["To"] = to_email

    text_lines = [
        f"- {p.title}\n  Precio: {p.price_label} | Tienda: {p.store_name}"
        + ("" if p.available else " | Stock sin confirmar")
        + f"\n  {p.url}"
        for p in products
    ]
    message.set_content("Ofertas disponibles:\n\n" + "\n\n".join(text_lines) + "\n")

    rows = "".join(
        "<tr>"
        f"<td>{escape(p.title)}</td>"
        f'<td style="text-align:right">{p.price_label}</td>'
        f"<td>{escape(p.store_name)}</td>"
        f'<td><a href="{escape(p.url, quote=True)}">Ver oferta</a></td>'
        "</tr>"
        for p in products
    )
    message.add_alternative(
        "<html><body><h2>Ofertas disponibles</h2>"
        '<table border="1" cellpadding="6" cellspacing="0">'
        "<tr><th>Producto</th><th>Precio</th><th>Tienda</th><th>Enlace</th></tr>"
        f"{rows}</table></body></html>",
        subtype="html",
    )
    return message


async def send_telegram_alert(
    products: list[Product],
    bot_token: str,
    chat_id: str,
    *,
    max_price: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Envía las ofertas disponibles a un chat de Telegram.

    Args:
        products: Resultados de la búsqueda.
        bot_token: Token del bot.
        chat_id: Chat de destino.
        max_price: Precio máximo para alertar; ``None`` no filtra por precio.
        client: Cliente HTTP a reutilizar; si es ``None`` se crea uno temporal.

    Returns:
        ``True`` si se envió, ``False`` si no había ofertas que alertar.

    Raises:
        NotificationError: Si Telegram rechaza el mensaje o falla la red.
    """
    offers = filter_alertable(products, max_price)
    if not offers:
        return False

    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": format_telegram_message(offers),
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as owned:
                response = await owned.post(url, json=payload)
        else:
            response = await client.post(url, json=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200].replace(bot_token, "***")
        raise NotificationError(
            f"Telegram respondió {exc.response.status_code}: {detail}"
        ) from None
    except httpx.HTTPError as exc:
        reason = str(exc).replace(bot_token, "***")
        raise NotificationError(
            f"Telegram: error de red ({type(exc).__name__}: {reason})"
        ) from None
    return True


async def send_email_alert(
    products: list[Product],
    smtp_config: SmtpConfig,
    to_email: str,
    *,
    max_price: float | None = None,
) -> bool:
    """Envía las ofertas disponibles por email.

    El envío SMTP (bloqueante) se ejecuta en un hilo aparte.

    Args:
        products: Resultados de la búsqueda.
        smtp_config: Servidor y credenciales SMTP.
        to_email: Destinatario.
        max_price: Precio máximo para alertar; ``None`` no filtra por precio.

    Returns:
        ``True`` si se envió, ``False`` si no había ofertas que alertar.

    Raises:
        ValueError: Si no hay remitente (``from_email`` ni ``username``).
        NotificationError: Si el servidor SMTP falla.
    """
    offers = filter_alertable(products, max_price)
    if not offers:
        return False

    from_email = smtp_config.get("from_email") or smtp_config.get("username")
    if not from_email:
        raise ValueError("smtp_config necesita 'from_email' o 'username'")

    message = build_email(offers, from_email, to_email)
    try:
        await asyncio.to_thread(_send_smtp, message, smtp_config)
    except (smtplib.SMTPException, OSError) as exc:
        raise NotificationError(
            f"Email: {type(exc).__name__}: {exc}".replace(
                smtp_config.get("password") or "\0", "***"
            )
        ) from None
    return True


def _send_smtp(message: EmailMessage, config: SmtpConfig) -> None:
    context = ssl.create_default_context()
    host = config["host"]
    if config.get("use_ssl"):
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            host, config.get("port", 465), timeout=REQUEST_TIMEOUT, context=context
        )
    else:
        server = smtplib.SMTP(host, config.get("port", 587), timeout=REQUEST_TIMEOUT)
    with server:
        if not config.get("use_ssl"):
            server.starttls(context=context)
        if username := config.get("username"):
            server.login(username, config.get("password", ""))
        server.send_message(message)
