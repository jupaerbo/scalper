import asyncio
import json
import smtplib
from email.message import EmailMessage
from typing import Any, Self

import httpx
import pytest

import notifier
from models import Product
from notifier import (
    NotificationError,
    SmtpConfig,
    build_email,
    filter_alertable,
    format_telegram_message,
    send_email_alert,
    send_telegram_alert,
    smtp_config_from_env,
)

TOKEN = "123456:SECRET-token"
PRODUCTS = [
    Product("RTX 4070 <OC> & Co", 589.9, True, "Tienda A", "https://a.test/p?x=1&y=2"),
    Product("RTX 4070 agotada", 450.0, False, "Tienda B", "https://b.test/agotada"),
    Product("RTX 4070 cara", 650.0, True, "Tienda B", "https://b.test/cara"),
    Product("RTX 4070 barata", 549.0, True, "Tienda B", "https://b.test/barata"),
]
SMTP: SmtpConfig = {
    "host": "smtp.test",
    "username": "bot@test.com",
    "password": "s3cr3t",
}


def test_filter_alertable_requires_stock_and_max_price() -> None:
    titles = [p.title for p in filter_alertable(PRODUCTS, 600.0)]

    assert titles == ["RTX 4070 barata", "RTX 4070 <OC> & Co"]


def test_filter_alertable_without_max_price_keeps_all_available() -> None:
    assert len(filter_alertable(PRODUCTS)) == 3


# --- Telegram -----------------------------------------------------------------


def test_telegram_message_formats_title_price_store_and_link() -> None:
    message = format_telegram_message(filter_alertable(PRODUCTS, 600.0))

    assert message.startswith("🚨 <b>¡Oferta encontrada con stock!</b>\n\n")
    assert (
        "• <b>RTX 4070 &lt;OC&gt; &amp; Co</b>\n"
        "  💰 Precio: 589.90€\n"
        "  🏪 Tienda: Tienda A\n"
        '  🔗 <a href="https://a.test/p?x=1&amp;y=2">Ver oferta</a>'
    ) in message
    assert message.index("barata") < message.index("&lt;OC&gt;")
    assert "agotada" not in message
    assert "cara" not in message


def test_telegram_message_is_truncated_to_limit() -> None:
    many = [
        Product(f"Producto {i} " + "x" * 80, 10.0, True, "T", f"https://t.test/{i}")
        for i in range(100)
    ]

    message = format_telegram_message(many)

    assert len(message) <= notifier.TELEGRAM_MAX_LENGTH
    assert message.rstrip().endswith("más")
    shown = message.count("<a href=")
    assert f"… y {100 - shown} más" in message


def _telegram(
    handler: Any, products: list[Product] = PRODUCTS, max_price: float = 600.0
) -> bool:
    async def run() -> bool:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await send_telegram_alert(
                products, TOKEN, "42", max_price=max_price, client=client
            )

    return asyncio.run(run())


def test_send_telegram_posts_html_message() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    assert _telegram(handler) is True

    (request,) = sent
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = json.loads(request.content)
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is False
    assert payload["text"] == format_telegram_message(filter_alertable(PRODUCTS, 600))


def test_send_telegram_skips_when_nothing_available() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no debería enviar nada")

    assert _telegram(handler, max_price=500.0) is False
    assert _telegram(handler, products=[]) is False


def test_send_telegram_error_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"description": f"Unauthorized {TOKEN}"})

    with pytest.raises(NotificationError, match="401") as info:
        _telegram(handler)

    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None


def test_send_telegram_network_error_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no route to {request.url}")

    with pytest.raises(NotificationError, match="error de red") as info:
        _telegram(handler)

    assert TOKEN not in str(info.value)


# --- Email --------------------------------------------------------------------


class FakeSMTP:
    """Sustituto de ``smtplib.SMTP`` que registra lo que se haría."""

    instances: list["FakeSMTP"] = []
    fail_with: Exception | None = None

    def __init__(self, host: str, port: int, **kwargs: Any) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.calls.append("quit")

    def starttls(self, **_: Any) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.calls.append("send")
        self.sent.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    FakeSMTP.fail_with = None
    monkeypatch.setattr(notifier.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def _email(config: SmtpConfig = SMTP, max_price: float = 600.0) -> bool:
    return asyncio.run(
        send_email_alert(PRODUCTS, config, "yo@test.com", max_price=max_price)
    )


def test_email_body_formats_title_price_store_and_link() -> None:
    message = build_email(filter_alertable(PRODUCTS, 600.0), "bot@test.com", "yo@x")

    assert message["Subject"] == "Alerta de precio: 2 oferta(s) disponible(s)"
    text = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert "- RTX 4070 <OC> & Co\n  Precio: 589.90€ | Tienda: Tienda A" in text
    assert "https://a.test/p?x=1&y=2" in text
    assert "<td>RTX 4070 &lt;OC&gt; &amp; Co</td>" in html
    assert '<a href="https://a.test/p?x=1&amp;y=2">Ver oferta</a>' in html
    assert "agotada" not in text
    assert "cara" not in text


def test_send_email_uses_starttls_and_login(fake_smtp: type[FakeSMTP]) -> None:
    assert _email() is True

    (server,) = fake_smtp.instances
    assert (server.host, server.port) == ("smtp.test", 587)
    assert server.calls == ["starttls", "login", "send", "quit"]
    assert server.login_args == ("bot@test.com", "s3cr3t")
    (message,) = server.sent
    assert message["From"] == "bot@test.com"
    assert message["To"] == "yo@test.com"


def test_send_email_with_ssl_skips_starttls(fake_smtp: type[FakeSMTP]) -> None:
    _email({**SMTP, "use_ssl": True, "from_email": "alertas@test.com"})

    (server,) = fake_smtp.instances
    assert server.port == 465
    assert "starttls" not in server.calls
    assert server.sent[0]["From"] == "alertas@test.com"


def test_send_email_skips_when_nothing_available(fake_smtp: type[FakeSMTP]) -> None:
    assert _email(max_price=100.0) is False
    assert fake_smtp.instances == []


def test_send_email_error_hides_password(fake_smtp: type[FakeSMTP]) -> None:
    fake_smtp.fail_with = smtplib.SMTPAuthenticationError(535, b"bad s3cr3t")

    with pytest.raises(NotificationError, match="SMTPAuthenticationError") as info:
        _email()

    assert "s3cr3t" not in str(info.value)


def test_send_email_requires_sender() -> None:
    with pytest.raises(ValueError, match="from_email"):
        _email({"host": "smtp.test"})


def test_smtp_config_from_env() -> None:
    env = {
        "SMTP_HOST": "smtp.test",
        "SMTP_PORT": "465",
        "SMTP_USER": "u",
        "SMTP_PASSWORD": "p",
        "SMTP_FROM": "f@test.com",
        "SMTP_SSL": "true",
    }

    assert smtp_config_from_env(env) == {
        "host": "smtp.test",
        "port": 465,
        "username": "u",
        "password": "p",
        "from_email": "f@test.com",
        "use_ssl": True,
    }
    assert smtp_config_from_env({}) is None
