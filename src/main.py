"""CLI de búsqueda de productos."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from exporter import SUPPORTED_FORMATS, export_products
from models import Product
from notifier import (
    NotificationError,
    send_email_alert,
    send_telegram_alert,
    smtp_config_from_env,
)
from scraper import SearchError, search_products

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
STOCK_LABELS = {
    True: "[green]Disponible[/]",
    False: "[red]Agotado[/]",
    None: "[yellow]Sin confirmar[/]",
}

app = typer.Typer(add_completion=False, help="Busca productos por debajo de un precio.")
console = Console()


def build_table(products: list[Product], max_price: float) -> Table:
    """Construye la tabla de resultados.

    Args:
        products: Productos a mostrar.
        max_price: Precio máximo usado en la búsqueda, para el título.

    Returns:
        Tabla de rich lista para imprimir.
    """
    table = Table(title=f"Resultados (≤ {max_price:.2f})")
    table.add_column("Tienda", style="cyan")
    table.add_column("Producto", style="bold")
    table.add_column("Precio", justify="right", style="green")
    table.add_column("Estado de Stock")
    table.add_column("Enlace", overflow="fold")
    for product in products:
        table.add_row(
            product.store_name,
            product.title,
            product.price_label,
            STOCK_LABELS[product.available],
            f"[link={product.url}]{product.url}[/link]",
        )
    return table


@app.command()
def search(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Option("--name", "-n", help="Nombre del producto a buscar.")
    ],
    max_price: Annotated[
        float, typer.Option("--max-price", "-p", min=0, help="Precio límite.")
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            dir_okay=False,
            help="Exporta los resultados a un archivo .json o .csv.",
        ),
    ] = None,
    telegram_token: Annotated[
        str | None,
        typer.Option(
            "--telegram-token",
            envvar="TELEGRAM_BOT_TOKEN",
            show_envvar=True,
            show_default=False,
            help="Token del bot de Telegram (mejor en .env que en la línea).",
        ),
    ] = None,
    telegram_chat_id: Annotated[
        str | None,
        typer.Option(
            "--telegram-chat-id",
            envvar="TELEGRAM_CHAT_ID",
            show_envvar=True,
            help="Chat de Telegram que recibe la alerta.",
        ),
    ] = None,
    notify_email: Annotated[
        str | None,
        typer.Option(
            "--notify-email",
            envvar="NOTIFY_EMAIL",
            show_envvar=True,
            help="Email que recibe la alerta. Requiere SMTP_* en el entorno.",
        ),
    ] = None,
) -> None:
    """Busca NAME y muestra los productos con precio ≤ MAX_PRICE."""
    if not name.strip():
        raise typer.BadParameter("no puede estar vacío", param_hint="--name")
    if output is not None and output.suffix.lower() not in SUPPORTED_FORMATS:
        raise typer.BadParameter(
            f"la extensión debe ser {' o '.join(SUPPORTED_FORMATS)}",
            param_hint="--output",
        )
    if bool(telegram_token) != bool(telegram_chat_id):
        if _from_env(ctx, "telegram_token") or _from_env(ctx, "telegram_chat_id"):
            # Típico: .env tiene el token del bot interactivo pero no un chat
            # para alertas. No es un error; simplemente no se avisa por Telegram.
            telegram_token = telegram_chat_id = None
        else:
            raise typer.BadParameter(
                "se necesitan ambos para Telegram",
                param_hint="--telegram-token/--telegram-chat-id",
            )
    smtp_config = None
    if notify_email:
        try:
            smtp_config = smtp_config_from_env()
        except ValueError as exc:
            raise typer.BadParameter(
                "SMTP_PORT debe ser un número", param_hint="--notify-email"
            ) from exc
        if smtp_config is None:
            raise typer.BadParameter(
                "define SMTP_HOST (y SMTP_USER/SMTP_PASSWORD) en .env",
                param_hint="--notify-email",
            )

    try:
        with console.status(f"Buscando [bold]{name}[/bold]..."):
            products = asyncio.run(search_products(name, max_price))
    except SearchError as exc:
        console.print(f"[red]Error de red:[/red] {exc}.")
        raise typer.Exit(code=1) from exc

    if products:
        console.print(build_table(products, max_price))
    else:
        console.print(f"[yellow]Sin resultados para '{name}' ≤ {max_price:.2f}.[/]")

    if output is not None:
        try:
            path = export_products(products, output)
        except OSError as exc:
            console.print(f"[red]No se pudo guardar {output}:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(
            f"[green]✔[/green] {len(products)} resultado(s) exportado(s) a "
            f"[bold]{path}[/bold]"
        )

    channels: dict[str, Coroutine[Any, Any, bool]] = {}
    if telegram_token and telegram_chat_id:
        channels["Telegram"] = send_telegram_alert(
            products, telegram_token, telegram_chat_id, max_price=max_price
        )
    if notify_email and smtp_config:
        channels["Email"] = send_email_alert(
            products, smtp_config, notify_email, max_price=max_price
        )
    if channels and not _notify(channels):
        raise typer.Exit(code=1)


def _from_env(ctx: typer.Context, param: str) -> bool:
    """Indica si el valor de ``param`` vino de una variable de entorno."""
    # Typer 0.28 incluye su propio click; se compara por nombre para no
    # depender de un módulo privado.
    source = ctx.get_parameter_source(param)
    return getattr(source, "name", None) == "ENVIRONMENT"


def _notify(channels: dict[str, Coroutine[Any, Any, bool]]) -> bool:
    """Envía las alertas en paralelo e informa del resultado de cada canal.

    Returns:
        ``True`` si ningún canal falló.
    """

    async def run() -> list[bool | BaseException]:
        return await asyncio.gather(*channels.values(), return_exceptions=True)

    ok = True
    for channel, result in zip(channels, asyncio.run(run()), strict=True):
        if isinstance(result, NotificationError | ValueError):
            console.print(f"[red]✘ {channel}:[/red] {result}")
            ok = False
        elif isinstance(result, BaseException):
            raise result
        elif result:
            console.print(f"[green]✔[/green] Alerta enviada por {channel}.")
        else:
            console.print(
                f"[dim]{channel}: sin ofertas disponibles, no se envía alerta.[/]"
            )
    return ok


if __name__ == "__main__":
    load_dotenv(ENV_FILE)
    app()
