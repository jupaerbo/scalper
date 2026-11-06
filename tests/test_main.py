import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import main
from models import Product
from scraper import SearchError

runner = CliRunner()

PRODUCTS = [
    Product("Teclado A", 19.5, True, "Tienda A", "https://a.test/a"),
    Product("Teclado B", 42.0, False, "Tienda B", "https://b.test/b"),
]


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "200")


def _patch_search(
    monkeypatch: pytest.MonkeyPatch, result: list[Product] | Exception
) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []

    async def fake(name: str, max_price: float) -> list[Product]:
        calls.append((name, max_price))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(main, "search_products", fake)
    return calls


def test_prints_results_table(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, PRODUCTS)

    result = runner.invoke(main.app, ["--name", "teclado", "--max-price", "50"])

    assert result.exit_code == 0, result.output
    assert calls == [("teclado", 50.0)]
    header = next(line for line in result.output.splitlines() if "Tienda" in line)
    columns = ["Tienda", "Producto", "Precio", "Estado de Stock", "Enlace"]
    assert [header.index(c) for c in columns] == sorted(
        header.index(c) for c in columns
    )
    for text in ("Tienda A", "Teclado A", "19.50", "Disponible", "https://a.test/a"):
        assert text in result.output
    for text in ("Tienda B", "Teclado B", "42.00", "Agotado", "https://b.test/b"):
        assert text in result.output


def test_short_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, [])

    result = runner.invoke(main.app, ["-n", "teclado", "-p", "5"])

    assert result.exit_code == 0
    assert calls == [("teclado", 5.0)]
    assert "Sin resultados" in result.output


def test_exits_with_error_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search(monkeypatch, SearchError("no hay conexión a internet"))

    result = runner.invoke(main.app, ["--name", "teclado", "--max-price", "50"])

    assert result.exit_code == 1
    assert "Error de red" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["--max-price", "50"],
        ["--name", "teclado"],
        ["--name", "teclado", "--max-price", "-1"],
        ["--name", "   ", "--max-price", "50"],
    ],
)
def test_rejects_missing_or_invalid_options(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    calls = _patch_search(monkeypatch, PRODUCTS)

    result = runner.invoke(main.app, args)

    assert result.exit_code == 2
    assert calls == []


def test_output_exports_json_and_confirms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_search(monkeypatch, PRODUCTS)
    target = tmp_path / "resultados.json"

    result = runner.invoke(main.app, ["-n", "rtx 4070", "-p", "600", "-o", str(target)])

    assert result.exit_code == 0, result.output
    assert "Teclado A" in result.output
    assert "2 resultado(s) exportado(s)" in result.output
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [row["title"] for row in data] == ["Teclado A", "Teclado B"]


def test_output_exports_csv_even_without_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_search(monkeypatch, [])
    target = tmp_path / "vacio.csv"

    result = runner.invoke(
        main.app, ["--name", "x", "--max-price", "1", "--output", str(target)]
    )

    assert result.exit_code == 0, result.output
    assert "0 resultado(s) exportado(s)" in result.output
    assert target.read_text(encoding="utf-8-sig").startswith("title,price")


def test_output_rejects_unsupported_extension_before_searching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_search(monkeypatch, PRODUCTS)

    result = runner.invoke(
        main.app, ["-n", "x", "-p", "1", "-o", str(tmp_path / "out.txt")]
    )

    assert result.exit_code == 2
    assert calls == []


def test_output_write_error_exits_with_code_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_search(monkeypatch, PRODUCTS)

    def fail(*_: object) -> Path:
        raise PermissionError("acceso denegado")

    monkeypatch.setattr(main, "export_products", fail)

    result = runner.invoke(
        main.app, ["-n", "x", "-p", "1", "-o", str(tmp_path / "out.json")]
    )

    assert result.exit_code == 1
    assert "No se pudo guardar" in result.output


# --- Alertas ------------------------------------------------------------------

ALERT_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "NOTIFY_EMAIL",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
)


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[tuple[object, ...]]]:
    for var in ALERT_ENV:
        monkeypatch.delenv(var, raising=False)
    sent: dict[str, list[tuple[object, ...]]] = {"telegram": [], "email": []}

    async def fake_telegram(
        products: list[Product], token: str, chat_id: str, *, max_price: float
    ) -> bool:
        sent["telegram"].append((token, chat_id, max_price, len(products)))
        return True

    async def fake_email(
        products: list[Product], config: dict[str, object], to: str, *, max_price: float
    ) -> bool:
        sent["email"].append((config["host"], to, max_price))
        return True

    monkeypatch.setattr(main, "send_telegram_alert", fake_telegram)
    monkeypatch.setattr(main, "send_email_alert", fake_email)
    return sent


def test_alerts_use_cli_options(
    monkeypatch: pytest.MonkeyPatch, alerts: dict[str, list[tuple[object, ...]]]
) -> None:
    _patch_search(monkeypatch, PRODUCTS)

    result = runner.invoke(
        main.app,
        ["-n", "x", "-p", "50", "--telegram-token", "T", "--telegram-chat-id", "42"],
    )

    assert result.exit_code == 0, result.output
    assert alerts["telegram"] == [("T", "42", 50.0, 2)]
    assert alerts["email"] == []
    assert "Alerta enviada por Telegram" in result.output


def test_alerts_read_from_environment(
    monkeypatch: pytest.MonkeyPatch, alerts: dict[str, list[tuple[object, ...]]]
) -> None:
    _patch_search(monkeypatch, PRODUCTS)
    env = {
        "TELEGRAM_BOT_TOKEN": "T",
        "TELEGRAM_CHAT_ID": "42",
        "NOTIFY_EMAIL": "yo@test.com",
        "SMTP_HOST": "smtp.test",
        "SMTP_USER": "bot@test.com",
    }

    result = runner.invoke(main.app, ["-n", "x", "-p", "50"], env=env)

    assert result.exit_code == 0, result.output
    assert alerts["telegram"] == [("T", "42", 50.0, 2)]
    assert alerts["email"] == [("smtp.test", "yo@test.com", 50.0)]


@pytest.mark.parametrize(
    "extra",
    [
        ["--telegram-token", "T"],
        ["--telegram-chat-id", "42"],
        ["--notify-email", "yo@test.com"],
    ],
)
def test_alerts_reject_incomplete_config_before_searching(
    monkeypatch: pytest.MonkeyPatch,
    alerts: dict[str, list[tuple[object, ...]]],
    extra: list[str],
) -> None:
    calls = _patch_search(monkeypatch, PRODUCTS)

    result = runner.invoke(main.app, ["-n", "x", "-p", "50", *extra])

    assert result.exit_code == 2
    assert calls == []


def test_alert_failure_exits_with_code_1(
    monkeypatch: pytest.MonkeyPatch, alerts: dict[str, list[tuple[object, ...]]]
) -> None:
    _patch_search(monkeypatch, PRODUCTS)

    async def failing(*_: object, **__: object) -> bool:
        raise main.NotificationError("Telegram respondió 401")

    monkeypatch.setattr(main, "send_telegram_alert", failing)

    result = runner.invoke(
        main.app,
        ["-n", "x", "-p", "50", "--telegram-token", "T", "--telegram-chat-id", "42"],
    )

    assert result.exit_code == 1
    assert "Telegram respondió 401" in result.output
    assert "Teclado A" in result.output


def test_token_only_in_env_skips_telegram_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch, alerts: dict[str, list[tuple[object, ...]]]
) -> None:
    _patch_search(monkeypatch, PRODUCTS)

    result = runner.invoke(
        main.app, ["-n", "x", "-p", "50"], env={"TELEGRAM_BOT_TOKEN": "T"}
    )

    assert result.exit_code == 0, result.output
    assert alerts["telegram"] == []
