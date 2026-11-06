import asyncio
import re
from collections.abc import Iterator

import httpx
import pytest

import scraper
from models import Product
from scraper import (
    SearchError,
    browser_headers,
    clean_title,
    create_client,
    describe_network_error,
    detect_stock,
    extract_price,
    extract_price_and_currency,
    extract_store,
    fetch_page,
    parse_price,
    parse_web_result,
    polite_delay,
    random_user_agent,
    search_products,
)

WEB_RESULTS = [
    {
        "title": "GeForce RTX 4070 Precio en España — Historial",
        "href": "https://gpuprix.com/es/spain/gpus/geforce-rtx-4070",
        "body": "Precio de GeForce RTX 4070 en España: €549 EUR · En stock, 12 GB",
    },
    {
        "title": "MSI RTX 4070 Ventus 2X | Tienda Ejemplo",
        "href": "https://www.tienda-ejemplo.es/msi-rtx-4070",
        "body": "Tarjeta gráfica RTX 4070 por 599,90 € IVA incluido. Agotado.",
    },
    {
        "title": "GigaByte GeForce RTX 4070 desde 1.800,00 - idealo",
        "href": "https://www.idealo.es/precios/202646842/gigabyte-geforce-rtx-4070.html",
        "body": "Tarjetas gráficas al mejor precio en idealo.es",
    },
    {
        "title": "RTX 4070 Eagle OC",
        "href": "https://www.otra.es/eagle",
        "body": "Oferta: 575 euros, envío gratis.",
    },
    {
        "title": "RTX 4070 Founders Edition - US Store",
        "href": "https://shop.example.com/rtx-4070",
        "body": "Now $499.99 USD. In stock.",
    },
    {
        "title": "Cloud GPU RTX 4070",
        "href": "https://cloud.example.com/rtx",
        "body": "Alquila una RTX 4070 desde 0,09 €/hora.",
    },
    {
        "title": "Nvidia RTX - Wikipedia",
        "href": "https://es.wikipedia.org/wiki/Nvidia_RTX",
        "body": "RTX es una plataforma de trazado de rayos en tiempo real.",
    },
    {
        "title": "Duplicado",
        "href": "https://gpuprix.com/es/spain/gpus/geforce-rtx-4070",
        "body": "€100",
    },
    {"title": "", "href": "https://vacio.es", "body": "100 €"},
]


@pytest.fixture(autouse=True)
def no_real_page_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las páginas de los resultados responden 404 salvo que el test lo cambie."""
    real_create_client = scraper.create_client

    def fake_create_client(
        transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0
    ) -> httpx.AsyncClient:
        fallback = httpx.MockTransport(lambda _: httpx.Response(404))
        return real_create_client(transport or fallback, timeout)

    monkeypatch.setattr(scraper, "create_client", fake_create_client)


@pytest.fixture(autouse=True)
def no_backoff_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Los reintentos del buscador no esperan en los tests."""
    monkeypatch.setattr(scraper, "BACKOFF_BASE", 0.0)


@pytest.fixture(autouse=True)
def no_search_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """La API de pago está desactivada salvo que el test la configure."""
    for var in ("SEARCH_API_KEY", "SEARCH_API_PROVIDER", "GOOGLE_CSE_ID"):
        monkeypatch.delenv(var, raising=False)


def _search(
    max_price: float, results: list[dict[str, str]] = WEB_RESULTS, **kwargs: bool
) -> tuple[list[Product], list[str]]:
    queries: list[str] = []

    def fake_search(query: str) -> list[dict[str, str]]:
        queries.append(query)
        return results

    products = asyncio.run(
        search_products("  rtx 4070 ", max_price, search_fn=fake_search, **kwargs)
    )
    return products, queries


# --- search_products ---------------------------------------------------------


def test_extracts_fields_and_filters_by_max_price() -> None:
    products, queries = _search(600.0)

    # Menos de MIN_PRODUCTS en la primera consulta: se prueba la segunda y los
    # resultados repetidos no se duplican.
    assert queries == ["rtx 4070 comprar precio €", "rtx 4070 oferta tienda online €"]
    assert products == [
        Product(
            "RTX 4070 Founders Edition",
            499.99,
            True,
            "shop.example.com",
            "https://shop.example.com/rtx-4070",
            "USD",
        ),
        Product(
            "GeForce RTX 4070 Precio en España",
            549.0,
            True,
            "gpuprix.com",
            "https://gpuprix.com/es/spain/gpus/geforce-rtx-4070",
        ),
        Product(
            "RTX 4070 Eagle OC", 575.0, None, "otra.es", "https://www.otra.es/eagle"
        ),
        Product(
            "MSI RTX 4070 Ventus 2X",
            599.9,
            False,
            "tienda-ejemplo.es",
            "https://www.tienda-ejemplo.es/msi-rtx-4070",
        ),
    ]


def test_max_price_is_inclusive() -> None:
    products, _ = _search(549.0)

    assert [p.price for p in products] == [499.99, 549.0]


def test_includes_desde_prices_and_drops_priceless_results() -> None:
    products, _ = _search(2000.0)

    stores = {p.store_name: p for p in products}
    assert stores["idealo.es"].price == 1800.0
    assert "es.wikipedia.org" not in stores
    assert "cloud.example.com" not in stores
    assert "vacio.es" not in stores


def test_only_available_drops_known_out_of_stock() -> None:
    products, _ = _search(600.0, only_available=True)

    assert "tienda-ejemplo.es" not in [p.store_name for p in products]
    assert len(products) == 3


def test_returns_empty_list_without_matches() -> None:
    assert _search(10.0)[0] == []
    assert _search(1000.0, results=[])[0] == []


@pytest.mark.parametrize(
    ("name", "max_price"), [("", 10.0), ("   ", 10.0), ("rtx", -1.0)]
)
def test_rejects_invalid_arguments(name: str, max_price: float) -> None:
    with pytest.raises(ValueError):
        asyncio.run(search_products(name, max_price, search_fn=lambda _: []))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OSError("[Errno 11001] getaddrinfo failed"), "DNS"),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "DNS"),
        (RuntimeError("network unreachable"), "conexión"),
        (RuntimeError("algo raro"), "no se pudo completar la búsqueda"),
    ],
)
def test_wraps_search_errors_in_clear_message(error: Exception, expected: str) -> None:
    def failing(query: str) -> list[dict[str, str]]:
        raise error

    with pytest.raises(SearchError, match=expected):
        asyncio.run(search_products("rtx", 100.0, search_fn=failing))


@pytest.mark.parametrize(
    ("exception_name", "expected"),
    [
        ("RatelimitException", "limitado las peticiones"),
        ("TimeoutException", "tardó demasiado"),
    ],
)
def test_search_engine_errors(exception_name: str, expected: str) -> None:
    import ddgs.exceptions

    error = getattr(ddgs.exceptions, exception_name)("x")

    def failing(query: str) -> list[dict[str, str]]:
        raise error

    with pytest.raises(SearchError, match=expected):
        asyncio.run(search_products("rtx", 100.0, search_fn=failing))


# --- Extracción --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Precio: 599,90 €", (599.9, "EUR")),
        ("€1100 EUR", (1100.0, "EUR")),
        ("Ahora 1.299 EUR", (1299.0, "EUR")),
        ("solo 99 euros", (99.0, "EUR")),
        ("desde 1.800,00 - idealo", (1800.0, "EUR")),
        ("RTX 4070 599 €", (599.0, "EUR")),
        ("Now $1,299.99", (1299.99, "USD")),
        ("499 USD shipped", (499.0, "USD")),
        ("US$ 549", (549.0, "USD")),
        ("350 dólares", (350.0, "USD")),
        ("$520 o 499 €", (499.0, "EUR")),
        ("Alquiler 0,09 €/h o compra por 549 €", (549.0, "EUR")),
        ("RTX 4070 con 12 GB", None),
    ],
)
def test_extract_price_and_currency(
    text: str, expected: tuple[float, str] | None
) -> None:
    assert extract_price_and_currency(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Alquiler desde 0,09 €/hora",
        "Cloud GPU $0.35 / h",
        "Suscripción 15 € al mes",
        "Precio 0,50 €",
    ],
)
def test_extract_price_ignores_rates_and_tiny_amounts(text: str) -> None:
    assert extract_price(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9,99 €", 9.99),
        ("1.299,99 €", 1299.99),
        ("$1,299.99", 1299.99),
        ("50.00", 50.0),
        ("1.299", 1299.0),
        ("1574.44", 1574.44),
        ("1.299.000", 1299000.0),
        ("Precio: 15", 15.0),
        ("Consultar", None),
    ],
)
def test_parse_price(text: str, expected: float | None) -> None:
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("En stock, envío 24h", True),
        ("Disponible en tienda", True),
        ("Producto AGOTADO", False),
        ("No disponible temporalmente", False),
        ("Sold out", False),
        ("12 GB GDDR6X", None),
    ],
)
def test_detect_stock(text: str, expected: bool | None) -> None:
    assert detect_stock(text) is expected


def test_extract_store_and_clean_title() -> None:
    assert extract_store("https://www.amazon.es/dp/X?y=1") == "amazon.es"
    assert extract_store("https://tienda.com") == "tienda.com"
    assert clean_title("MSI RTX 4070 | Tienda") == "MSI RTX 4070"
    assert clean_title("RTX 4070 — Historial") == "RTX 4070"
    assert clean_title("RTX-4070") == "RTX-4070"


def test_parse_web_result_rejects_invalid_urls() -> None:
    result = {"title": "x", "href": "javascript:alert(1)", "body": "5 €"}

    assert parse_web_result(result) is None


def test_price_label() -> None:
    assert Product("x", 589.9, True, "t", "u").price_label == "589.90€"
    assert Product("x", 499.99, True, "t", "u", "USD").price_label == "499.99 USD"


# --- Utilidades HTTP -----------------------------------------------------------

CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)
EDGE_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0"
)


@pytest.mark.parametrize(
    ("user_agent", "brand", "platform"),
    [
        (CHROME_WIN, '"Google Chrome";v="135"', '"Windows"'),
        (EDGE_MAC, '"Microsoft Edge";v="132"', '"macOS"'),
    ],
)
def test_browser_headers_match_user_agent(
    user_agent: str, brand: str, platform: str
) -> None:
    headers = browser_headers(user_agent)

    assert headers["User-Agent"] == user_agent
    assert headers["Accept-Language"].startswith("es-ES")
    assert brand in headers["Sec-Ch-Ua"]
    assert headers["Sec-Ch-Ua-Platform"] == platform


def test_fetch_page_waits_and_sends_dynamic_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents: Iterator[str] = iter([CHROME_WIN, EDGE_MAC])
    monkeypatch.setattr(scraper, "random_user_agent", lambda: next(agents))
    delays: list[None] = []

    async def fake_delay() -> None:
        delays.append(None)

    monkeypatch.setattr(scraper, "polite_delay", fake_delay)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert len(delays) == len(sent) + 1, "la petición va después del retardo"
        sent.append(request)
        return httpx.Response(200, text="ok")

    async def run() -> None:
        for _ in range(2):
            async with create_client(httpx.MockTransport(handler)) as client:
                await fetch_page("https://a.test/p", client, referer="https://a.test/")

    asyncio.run(run())

    first, second = (r.headers for r in sent)
    assert first["User-Agent"] == CHROME_WIN
    assert second["User-Agent"] == EDGE_MAC
    assert first["Referer"] == "https://a.test/"


def test_polite_delay_sleeps_random_time_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(scraper.asyncio, "sleep", fake_sleep)

    async def run() -> None:
        for _ in range(30):
            await polite_delay()

    asyncio.run(run())

    assert all(1.5 <= s <= 3.5 for s in slept)
    assert len(set(slept)) > 1


def test_random_user_agent_is_desktop_chromium() -> None:
    agents = {random_user_agent() for _ in range(20)}

    assert len(agents) > 1
    assert all("Chrome/" in a and "Mobile" not in a for a in agents)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), "DNS"),
        (httpx.ConnectError("[WinError 10061] refused"), "no se pudo conectar"),
        (httpx.ReadTimeout("timed out"), "tardó demasiado"),
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "https://t.test"),
                response=httpx.Response(429),
            ),
            "bloqueado la petición (HTTP 429)",
        ),
    ],
)
def test_describe_network_error(exc: httpx.HTTPError, expected: str) -> None:
    assert expected in describe_network_error(exc)


def test_title_price_wins_over_snippet_price() -> None:
    product = parse_web_result(
        {
            "title": "GigaByte GeForce RTX 4070 desde 786,02 - idealo",
            "href": "https://www.idealo.es/precios/1",
            "body": "El precio oficial de la Founders Edition es de 669€.",
        }
    )

    assert product is not None
    assert product.price == 786.02


# --- Cobertura: consultas, número de resultados y formatos ----------------------

from scraper import MAX_PRODUCTS, MIN_PRODUCTS, build_queries  # noqa: E402


def _offer(i: int, price: float = 100.0, extra: str = "") -> dict[str, str]:
    return {
        "title": f"Producto {i}",
        "href": f"https://tienda{i}.es/p",
        "body": f"Precio {price + i:.2f} € {extra}",
    }


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rtx 4070", ["rtx 4070 comprar precio €", "rtx 4070 oferta tienda online €"]),
        ("comprar rtx 4070", ["comprar rtx 4070", "comprar rtx 4070 precio"]),
        ("rtx 4070 precio", ["rtx 4070 precio", "rtx 4070 precio comprar"]),
        ("rtx 4070 €", ["rtx 4070 €", "rtx 4070 € comprar precio"]),
        (
            "compresor de aire",
            [
                "compresor de aire comprar precio €",
                "compresor de aire oferta tienda online €",
            ],
        ),
    ],
)
def test_build_queries_adds_purchase_terms_only_when_missing(
    name: str, expected: list[str]
) -> None:
    assert build_queries(name) == expected


def test_stops_after_first_query_when_enough_results() -> None:
    results = [_offer(i) for i in range(MIN_PRODUCTS)]

    products, queries = _search(1000.0, results=results)

    assert len(queries) == 1
    assert len(products) == MIN_PRODUCTS


def test_merges_queries_until_enough_results() -> None:
    pages = iter([[_offer(i) for i in range(4)], [_offer(i) for i in range(2, 12)]])
    queries: list[str] = []

    def fake_search(query: str) -> list[dict[str, str]]:
        queries.append(query)
        return next(pages)

    products = asyncio.run(search_products("rtx", 1000.0, search_fn=fake_search))

    assert len(queries) == 2
    assert len(products) == 12
    assert len({p.url for p in products}) == 12


def test_returns_at_most_limit_cheapest_first() -> None:
    results = [_offer(i) for i in range(40)]

    products, _ = _search(1000.0, results=results)

    assert len(products) == MAX_PRODUCTS
    assert [p.price for p in products] == sorted(p.price for p in products)
    assert products[0].price == 100.0


def test_later_query_failure_keeps_previous_results() -> None:
    calls = iter([[_offer(1), _offer(2)], OSError("[Errno 11001] getaddrinfo")])

    def flaky(query: str) -> list[dict[str, str]]:
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    products = asyncio.run(search_products("rtx", 1000.0, search_fn=flaky))

    assert [p.store_name for p in products] == ["tienda1.es", "tienda2.es"]


def test_unconfirmed_stock_is_kept_not_discarded() -> None:
    results = [
        _offer(1, extra="Envío gratis"),
        _offer(2, extra="En stock"),
        _offer(3, extra="Agotado"),
        _offer(4, extra="Sin stock"),
    ]

    products, _ = _search(1000.0, results=results)

    assert {p.store_name: p.available for p in products} == {
        "tienda2.es": True,
        "tienda1.es": None,
        "tienda3.es": False,
        "tienda4.es": False,
    }
    only, _ = _search(1000.0, results=results, only_available=True)
    assert {p.store_name for p in only} == {"tienda1.es", "tienda2.es"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("599,00 €", 599.0),
        ("599€", 599.0),
        ("599.99 EUR", 599.99),
        ("599 €", 599.0),
        ("599,99€", 599.99),
        ("1.599,00 €", 1599.0),
        ("1,599.00 EUR", 1599.0),
        ("599.- €", None),
        ("EUR 599,50", 599.5),
        ("Precio: 599 Euros", 599.0),
    ],
)
def test_extract_price_common_euro_formats(text: str, expected: float | None) -> None:
    assert extract_price(text) == expected


# --- Precio desde la página del resultado ---------------------------------------

from scraper import (  # noqa: E402
    MAX_PAGE_FETCHES,
    PagePrice,
    extract_price_from_html,
    fetch_page_price,
)

HTML_OG = """<html><head>
<meta property="og:title" content="RTX 4070">
<meta property="product:price:amount" content="589.90">
<meta property="product:price:currency" content="EUR">
<meta property="product:availability" content="in stock">
</head><body>Otros productos desde 19,99 €</body></html>"""

HTML_OG_USD = """<meta content="499.99" property="og:price:amount">
<meta content="usd" property="og:price:currency">"""

HTML_ITEMPROP = """<div itemscope itemtype="https://schema.org/Product">
<h1>RTX 4070</h1><span itemprop="price" content="1.299,00">1.299,00 €</span>
<link itemprop="availability" href="https://schema.org/OutOfStock"></div>"""

HTML_NAME_PRICE = (
    '<meta name="price" content="649"><meta name="priceCurrency" content="EUR">'
)

HTML_JSON_LD = """<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [
  {"@type": "BreadcrumbList"},
  {"@type": "Product", "name": "RTX 4070",
   "offers": {"@type": "Offer", "price": 579, "priceCurrency": "EUR",
              "availability": "https://schema.org/InStock"}}
]}</script>
<meta property="product:price:amount" content="999">"""

HTML_JSON_LD_AGGREGATE = """<script type="application/ld+json">
[{"@type": "Product", "offers": {"@type": "AggregateOffer", "lowPrice": "549.50",
  "priceCurrency": "EUR"}}]</script>"""

HTML_TEXT_ONLY = """<html><head><script>var p = "1 €";</script>
<style>.x{content:"2 €"}</style></head>
<body><h1>RTX 4070 Eagle</h1><p>Precio:&nbsp;612,95&nbsp;€</p>
<p>Agotado</p></body></html>"""


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (HTML_OG, PagePrice(589.9, "EUR", True)),
        (HTML_OG_USD, PagePrice(499.99, "USD", None)),
        (HTML_ITEMPROP, PagePrice(1299.0, "EUR", False)),
        (HTML_NAME_PRICE, PagePrice(649.0, "EUR", None)),
        (HTML_JSON_LD, PagePrice(579.0, "EUR", True)),
        (HTML_JSON_LD_AGGREGATE, PagePrice(549.5, "EUR", None)),
        (HTML_TEXT_ONLY, PagePrice(612.95, "EUR", False)),
        ("<html><body>Sin precio aquí</body></html>", None),
        ('<script type="application/ld+json">{roto</script>', None),
    ],
)
def test_extract_price_from_html(html: str, expected: PagePrice | None) -> None:
    assert extract_price_from_html(html) == expected


def _page_client(
    pages: dict[str, httpx.Response | Exception], delay: float = 0.0
) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if delay:
            await asyncio.sleep(delay)
        outcome = pages.get(str(request.url), httpx.Response(404))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html"})


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (_html(HTML_OG), 589.9),
        (
            httpx.Response(403, text=HTML_OG, headers={"content-type": "text/html"}),
            None,
        ),
        (httpx.Response(200, json={"price": 1}), None),
        (httpx.ConnectError("[Errno 11001] getaddrinfo failed"), None),
        (httpx.ReadTimeout("timed out"), None),
        (RuntimeError("inesperado"), None),
    ],
)
def test_fetch_page_price_never_raises(
    response: httpx.Response | Exception, expected: float | None
) -> None:
    async def run() -> PagePrice | None:
        async with _page_client({"https://t.es/p": response}) as client:
            return await fetch_page_price(client, "https://t.es/p")

    page = asyncio.run(run())

    assert (page.price if page else None) == expected


def test_fetch_page_price_gives_up_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scraper, "PAGE_TIMEOUT", 0.05)

    async def run() -> PagePrice | None:
        async with _page_client({"https://t.es/p": _html(HTML_OG)}, delay=1) as c:
            return await fetch_page_price(c, "https://t.es/p")

    assert asyncio.run(run()) is None


def _priceless(i: int) -> dict[str, str]:
    return {
        "title": f"Tienda {i} | RTX 4070",
        "href": f"https://t{i}.es/p",
        "body": "La mejor gráfica para jugar en 1440p.",
    }


def _search_with_pages(
    results: list[dict[str, str]],
    pages: dict[str, httpx.Response | Exception],
    max_price: float = 1000.0,
    delay: float = 0.0,
    **kwargs: bool,
) -> list[Product]:
    async def run() -> list[Product]:
        async with _page_client(pages, delay) as client:
            return await search_products(
                "rtx 4070",
                max_price,
                search_fn=lambda _: results,
                page_client=client,
                **kwargs,
            )

    return asyncio.run(run())


def test_priceless_results_get_price_from_their_page() -> None:
    results = [_priceless(1), _priceless(2), _priceless(3), _priceless(4)]
    pages = {
        "https://t1.es/p": _html(HTML_OG),
        "https://t2.es/p": _html(HTML_ITEMPROP),
        "https://t3.es/p": httpx.ConnectError("[Errno 11001] getaddrinfo failed"),
        "https://t4.es/p": httpx.Response(403),
    }

    products = _search_with_pages(results, pages, max_price=2000.0)

    assert products == [
        Product("Tienda 1", 589.9, True, "t1.es", "https://t1.es/p"),
        Product("Tienda 2", 1299.0, False, "t2.es", "https://t2.es/p"),
    ]


def test_page_prices_respect_max_price() -> None:
    pages = {"https://t2.es/p": _html(HTML_ITEMPROP)}

    assert _search_with_pages([_priceless(2)], pages, max_price=1000.0) == []


def test_snippet_price_is_not_overwritten_by_page() -> None:
    results = [{"title": "RTX", "href": "https://t1.es/p", "body": "Solo 500 €"}]
    pages = {"https://t1.es/p": _html(HTML_OG)}

    (product,) = _search_with_pages(results, pages)

    assert product.price == 500.0


def test_pages_are_fetched_concurrently() -> None:
    import time

    results = [_priceless(i) for i in range(6)]
    pages = {f"https://t{i}.es/p": _html(HTML_OG) for i in range(6)}

    start = time.perf_counter()
    products = _search_with_pages(results, pages, delay=0.3)
    elapsed = time.perf_counter() - start

    assert len(products) == 6
    assert elapsed < 1.0  # en serie serían ≥ 1,8 s


def test_fetches_at_most_max_page_fetches() -> None:
    results = [_priceless(i) for i in range(MAX_PAGE_FETCHES + 5)]
    pages = {f"https://t{i}.es/p": _html(HTML_OG) for i in range(len(results))}

    products = _search_with_pages(results, pages)

    assert len(products) == MAX_PAGE_FETCHES


def test_inspect_pages_can_be_disabled() -> None:
    pages = {"https://t1.es/p": _html(HTML_OG)}

    assert _search_with_pages([_priceless(1)], pages, inspect_pages=False) == []


# --- Filtro de listados / categorías ----------------------------------------

from scraper import is_listing_url  # noqa: E402

LISTING_URLS = [
    "https://t.es/categoria/tarjetas-graficas",
    "https://t.es/c/componentes/gpu",
    "https://t.es/listado/rtx-4070",
    "https://t.es/buscar/rtx+4070",
    "https://t.es/search/rtx-4070",
    "https://t.es/catalog/gpu",
    "https://t.es/es/Categoria/GPU/",
    "https://t.es/productos?q=rtx+4070",
    "https://t.es/tienda?search=rtx",
]


@pytest.mark.parametrize("url", LISTING_URLS)
def test_is_listing_url_detects_listings(url: str) -> None:
    assert is_listing_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://t.es/p/rtx-4070-eagle-12gb",
        "https://t.es/msi-geforce-rtx-4070-ventus",
        "https://t.es/cable/hdmi-2-1",  # /cable/ no es /c/
        "https://t.es/categoria-rtx-4070.html",  # no es un segmento completo
        "https://t.es/dp/B0C123?ref=search",  # "ref" no es una búsqueda
    ],
)
def test_is_listing_url_keeps_product_pages(url: str) -> None:
    assert not is_listing_url(url)


def _listing(i: int, url: str) -> dict[str, str]:
    return {**_priceless(i), "href": url}


def test_listing_urls_are_never_fetched() -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return _html(HTML_OG)

    results = [_listing(i, url) for i, url in enumerate(LISTING_URLS)]
    results.append(_priceless(99))

    async def run() -> list[Product]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await search_products(
                "rtx 4070", 1000.0, search_fn=lambda _: results, page_client=client
            )

    products = asyncio.run(run())

    assert requested == ["https://t99.es/p"]
    assert [p.url for p in products] == ["https://t99.es/p"]


def test_listings_do_not_use_up_page_fetch_slots() -> None:
    listings = [
        _listing(100 + i, f"https://l{i}.es/categoria/gpu")
        for i in range(MAX_PAGE_FETCHES)
    ]
    products_ = [_priceless(i) for i in range(3)]
    pages = {f"https://t{i}.es/p": _html(HTML_OG) for i in range(3)}

    products = _search_with_pages(listings + products_, pages)

    assert len(products) == 3


def test_redirect_to_listing_is_discarded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/p/agotado":
            return httpx.Response(
                301, headers={"location": "https://t.es/categoria/gpu"}
            )
        return _html(HTML_OG)

    async def run() -> PagePrice | None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True
        ) as client:
            return await fetch_page_price(client, "https://t.es/p/agotado")

    assert asyncio.run(run()) is None


def test_no_results_is_an_empty_search_not_an_error() -> None:
    from ddgs.exceptions import DDGSException

    calls = iter([DDGSException("No results found."), [_offer(1)]])

    def search_fn(query: str) -> list[dict[str, str]]:
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    products = asyncio.run(search_products("rtx", 1000.0, search_fn=search_fn))

    assert [p.store_name for p in products] == ["tienda1.es"]


def test_no_results_in_every_query_returns_empty_list() -> None:
    from ddgs.exceptions import DDGSException

    def search_fn(query: str) -> list[dict[str, str]]:
        raise DDGSException("No results found.")

    assert asyncio.run(search_products("rtx", 1000.0, search_fn=search_fn)) == []


# --- Precios en oferta / descuento ----------------------------------------------


def _ld(data: object) -> str:
    import json

    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def _product_ld(offers: object, **extra: object) -> str:
    return _ld({"@type": "Product", "name": "RTX 4070", "offers": offers, **extra})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Antes 799 € ahora 649 €", (649.0, "EUR")),
        ("PVPR 899,00 € · Precio 749,00 €", (749.0, "EUR")),
        ("Precio anterior: 1.099 € Oferta: 999 €", (999.0, "EUR")),
        ("Was $599 now $499", (499.0, "USD")),
        ("RTX 4070 799 € (rebajado 699 €)", (699.0, "EUR")),
        ("Antes: 799 €", None),
    ],
)
def test_text_prefers_sale_price_and_skips_old_price(
    text: str, expected: tuple[float, str] | None
) -> None:
    assert extract_price_and_currency(text) == expected


def test_snippet_sale_price_beats_title_price() -> None:
    product = parse_web_result(
        {
            "title": "MSI RTX 4070 Ventus 799 € | Tienda",
            "href": "https://tienda.es/msi",
            "body": "¡Rebajado! Ahora 649 € con envío gratis.",
        }
    )

    assert product is not None
    assert product.price == 649.0


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (_product_ld({"price": "799.00", "salePrice": "649.00"}), 649.0),
        (
            _product_ld(
                {
                    "priceCurrency": "EUR",
                    "priceSpecification": [
                        {
                            "@type": "UnitPriceSpecification",
                            "priceType": "https://schema.org/StrikethroughPrice",
                            "price": 899,
                        },
                        {"@type": "UnitPriceSpecification", "price": 699},
                    ],
                }
            ),
            699.0,
        ),
        (
            _product_ld(
                {
                    "@type": "AggregateOffer",
                    "lowPrice": 579,
                    "highPrice": 899,
                    "priceCurrency": "EUR",
                }
            ),
            579.0,
        ),
        (
            '<meta property="product:price:amount" content="799">'
            '<meta property="product:sale_price:amount" content="649">',
            649.0,
        ),
        (
            '<meta property="product:price:amount" content="799">'
            '<meta property="product:sale_price:amount" content="649">'
            '<meta property="product:sale_price_dates:end" content="2020-01-31">',
            799.0,
        ),
        (
            "<body><h1>RTX 4070</h1><del>799,00 €</del> <ins>649,00 €</ins></body>",
            649.0,
        ),
        ("<body>Antes 799 € · Ahora 649 €</body>", 649.0),
    ],
)
def test_html_extracts_current_sale_price(html: str, expected: float) -> None:
    page = extract_price_from_html(html)

    assert page is not None
    assert page.price == expected


def test_html_prefers_cheapest_offer_in_stock() -> None:
    html = _product_ld(
        [
            {"price": 500, "availability": "https://schema.org/OutOfStock"},
            {"price": 550, "availability": "https://schema.org/InStock"},
            {"price": 600, "availability": "https://schema.org/InStock"},
        ]
    )

    assert extract_price_from_html(html) == PagePrice(550.0, "EUR", True)


def test_html_reports_cheapest_out_of_stock_offer_if_none_in_stock() -> None:
    html = _product_ld(
        [
            {"price": 520, "availability": "OutOfStock"},
            {"price": 500, "availability": "Discontinued"},
        ]
    )

    assert extract_price_from_html(html) == PagePrice(500.0, "EUR", False)


def test_over_budget_snippet_is_rechecked_and_kept_if_sale_fits() -> None:
    results = [
        {
            "title": "MSI RTX 4070 - 799 € | Tienda",
            "href": "https://t1.es/p",
            "body": "Tarjeta gráfica MSI.",
        },
        {
            # Muy por encima del margen: no merece una descarga.
            "title": "RTX 4070 Pack - 2.000 €",
            "href": "https://t2.es/p",
            "body": "",
        },
    ]
    requested: list[str] = []
    sale_page = _html(_product_ld({"price": 799, "salePrice": 649}))

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return sale_page

    async def run() -> list[Product]:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await search_products(
                "rtx 4070", 700.0, search_fn=lambda _: results, page_client=client
            )

    products = asyncio.run(run())

    assert [(p.url, p.price) for p in products] == [("https://t1.es/p", 649.0)]
    assert requested == ["https://t1.es/p"]


def test_over_budget_snippet_stays_out_if_page_price_is_also_high() -> None:
    results = [{"title": "RTX 4070 799 €", "href": "https://t1.es/p", "body": ""}]
    pages = {"https://t1.es/p": _html(_product_ld({"price": 789}))}

    assert _search_with_pages(results, pages, max_price=700.0) == []


# --- Stock real ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Temporalmente agotado", False),
        ("Producto fuera de stock", False),
        ("No hay stock en este momento", False),
        ("Recibir aviso cuando esté disponible", False),
        ("Avísame cuando vuelva a estar disponible", False),
        ("Artículo descatalogado", False),
        ("Currently unavailable.", False),
        ("¡Últimas unidades!", True),
        ("Casi agotado, date prisa", True),
        ("Consultar disponibilidad", None),
    ],
)
def test_detect_stock_explicit_unavailability(text: str, expected: bool | None) -> None:
    assert detect_stock(text) is expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            _product_ld(
                {"price": 599, "availability": "https://schema.org/OutOfStock"}
            ),
            False,
        ),
        (
            _product_ld(
                {"price": 599, "availability": "http://schema.org/Discontinued"}
            ),
            False,
        ),
        (_product_ld({"price": 599, "availability": "SoldOut"}), False),
        (
            _product_ld({"price": 599, "availability": "https://schema.org/InStock"}),
            True,
        ),
        # Sin availability en JSON-LD: decide la metaetiqueta…
        (
            _product_ld({"price": 599})
            + '<meta property="product:availability" content="out of stock">',
            False,
        ),
        # …o, si tampoco hay, el texto visible.
        (_product_ld({"price": 599}) + "<p>Temporalmente agotado</p>", False),
        (_product_ld({"price": 599}) + "<p>Recibir aviso cuando haya stock</p>", False),
        # JSON-LD manda sobre el texto.
        (
            _product_ld({"price": 599, "availability": "InStock"})
            + "<p>Accesorios relacionados: agotado</p>",
            True,
        ),
        (_product_ld({"price": 599}) + "<p>Consultar disponibilidad</p>", None),
    ],
)
def test_html_detects_real_stock(html: str, expected: bool | None) -> None:
    page = extract_price_from_html(html)

    assert page is not None
    assert page.available is expected


def test_out_of_stock_page_is_marked_and_dropped_when_stock_required() -> None:
    pages = {
        "https://t1.es/p": _html(
            _product_ld({"price": 599, "availability": "https://schema.org/OutOfStock"})
        ),
        "https://t2.es/p": _html(
            _product_ld({"price": 610, "availability": "InStock"})
        ),
    }
    results = [_priceless(1), _priceless(2)]

    every = _search_with_pages(results, pages)
    only = _search_with_pages(results, pages, only_available=True)

    assert {p.url: p.available for p in every} == {
        "https://t2.es/p": True,
        "https://t1.es/p": False,
    }
    assert [p.url for p in only] == ["https://t2.es/p"]


def test_out_of_stock_page_is_excluded_from_alerts() -> None:
    from notifier import filter_alertable

    pages = {
        "https://t1.es/p": _html(
            _product_ld({"price": 599, "availability": "OutOfStock"})
        )
    }

    (product,) = _search_with_pages([_priceless(1)], pages)

    assert product.available is False
    assert filter_alertable([product]) == []


# --- Listados detectados por su JSON-LD ----------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        _ld(
            {
                "@type": "ItemList",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "item": {"@type": "Product", "offers": {"price": 1}},
                    }
                ],
            }
        )
        + '<meta property="product:price:amount" content="599">',
        _product_ld({"price": 599}) + _product_ld({"price": 649}),
    ],
)
def test_listing_pages_by_json_ld_have_no_price(html: str) -> None:
    assert extract_price_from_html(html) is None


def test_product_page_with_breadcrumb_and_related_products_is_not_a_listing() -> None:
    html = _ld(
        {
            "@graph": [
                {"@type": "BreadcrumbList"},
                {
                    "@type": "Product",
                    "offers": {"price": 599},
                    "isRelatedTo": [{"@type": "Product", "offers": {"price": 19}}],
                },
            ]
        }
    )

    assert extract_price_from_html(html) == PagePrice(599.0, "EUR", None)


# --- Rate-limit: reintentos, backoff y consultas flexibles ---------------------

import logging  # noqa: E402

from ddgs.exceptions import DDGSException, RatelimitException  # noqa: E402

from scraper import (  # noqa: E402
    BACKOFF_MAX,
    EMPTY_SEARCH_ATTEMPTS,
    SEARCH_ATTEMPTS,
    SEARCH_BACKENDS,
    backoff_delay,
    is_rate_limit,
    relaxed_queries,
    run_search,
)


class _Scripted:
    """search_fn que devuelve (o lanza) cada resultado de ``outcomes`` en orden."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[dict[str, str]]:
        self.queries.append(query)
        outcome = self.outcomes.pop(0) if self.outcomes else []
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(scraper.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(scraper, "BACKOFF_BASE", 1.0)
    return slept


def test_backoff_delay_grows_exponentially_with_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scraper, "BACKOFF_BASE", 1.0)
    for retry in range(6):
        ceiling = min(BACKOFF_MAX, 2**retry)
        delays = {backoff_delay(retry) for _ in range(20)}
        assert all(ceiling / 2 <= d <= ceiling for d in delays)
        assert len(delays) > 1  # jitter aleatorio


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RatelimitException("x"), True),
        (DDGSException("https://duckduckgo.com 202 Ratelimit"), True),
        (RuntimeError("HTTP 429 Too Many Requests"), True),
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "https://a.test"),
                response=httpx.Response(429),
            ),
            True,
        ),
        (OSError("[Errno 11001] getaddrinfo failed"), False),
        (DDGSException("No results found."), False),
    ],
)
def test_is_rate_limit(error: Exception, expected: bool) -> None:
    assert is_rate_limit(error) is expected


def test_run_search_retries_network_errors_with_backoff(sleeps: list[float]) -> None:
    fn = _Scripted(httpx.ConnectError("boom"), RatelimitException("x"), [_offer(1)])

    results = asyncio.run(run_search("rtx", fn))

    assert results == [_offer(1)]
    assert len(fn.queries) == 3
    assert 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0


def test_run_search_retries_empty_results_then_gives_up(sleeps: list[float]) -> None:
    fn = _Scripted([], DDGSException("No results found."), [_offer(1)])

    assert asyncio.run(run_search("rtx", fn)) == []
    assert len(fn.queries) == EMPTY_SEARCH_ATTEMPTS
    assert len(sleeps) == EMPTY_SEARCH_ATTEMPTS - 1


def test_run_search_raises_last_error_and_logs_rate_limit(
    sleeps: list[float], caplog: pytest.LogCaptureFixture
) -> None:
    fn = _Scripted(*[RatelimitException("202 Ratelimit")] * SEARCH_ATTEMPTS)

    with (
        caplog.at_level(logging.WARNING, logger="scraper"),
        pytest.raises(RatelimitException),
    ):
        asyncio.run(run_search("rtx", fn))

    assert len(fn.queries) == SEARCH_ATTEMPTS
    assert len(sleeps) == SEARCH_ATTEMPTS - 1
    assert "rate-limit" in caplog.text
    assert "DDGS_PROXY" in caplog.text


def test_run_search_rotates_ddgs_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    backends: list[str] = []

    def fake_ddg(query: str, backend: str = "auto") -> list[dict[str, str]]:
        backends.append(backend)
        if len(backends) < 3:
            raise RatelimitException("x")
        return [_offer(1)]

    monkeypatch.setattr(scraper, "_ddg_text", fake_ddg)

    assert asyncio.run(run_search("rtx")) == [_offer(1)]
    assert backends == list(SEARCH_BACKENDS)


def test_rate_limit_on_first_query_is_a_clean_search_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fn = _Scripted(*[RatelimitException("x")] * SEARCH_ATTEMPTS)

    with (
        caplog.at_level(logging.WARNING, logger="scraper"),
        pytest.raises(SearchError, match="limitado las peticiones"),
    ):
        asyncio.run(search_products("rtx", 1000.0, search_fn=fn))

    assert "rate-limit" in caplog.text


def test_rate_limit_in_later_query_keeps_results_and_stops() -> None:
    fn = _Scripted([_offer(1)], *[RatelimitException("x")] * SEARCH_ATTEMPTS)

    products = asyncio.run(search_products("rtx", 1000.0, search_fn=fn))

    assert [p.store_name for p in products] == ["tienda1.es"]
    # 1 consulta buena + los reintentos de la 2.ª; ni más consultas ni fallback.
    assert len(fn.queries) == 1 + SEARCH_ATTEMPTS


def test_first_query_recovers_after_transient_rate_limit() -> None:
    fn = _Scripted(RatelimitException("x"), [_offer(i) for i in range(MIN_PRODUCTS)])

    products = asyncio.run(search_products("rtx", 1000.0, search_fn=fn))

    assert len(products) == MIN_PRODUCTS


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rtx 4070", ["rtx 4070 precio", "rtx 4070"]),
        (
            "Comprar 'MSI RTX-4070 Ventus 2X' €",
            ["msi rtx 4070 ventus 2x precio", "msi rtx 4070 ventus 2x", "msi rtx 4070"],
        ),
        ('"iphone 15" -funda oferta', ["iphone 15 funda precio", "iphone 15 funda"]),
        ("comprar precio €", []),
    ],
)
def test_relaxed_queries(name: str, expected: list[str]) -> None:
    assert relaxed_queries(name) == expected


def test_zero_results_falls_back_to_relaxed_queries() -> None:
    queries: list[str] = []

    def search_fn(query: str) -> list[dict[str, str]]:
        queries.append(query)
        return [_offer(1)] if query == "msi rtx 4070" else []

    products = asyncio.run(
        search_products("MSI RTX 4070 Ventus 2X", 1000.0, search_fn=search_fn)
    )

    assert [p.store_name for p in products] == ["tienda1.es"]
    assert "msi rtx 4070 ventus 2x precio" in queries
    assert queries[-1] == "msi rtx 4070"


def test_no_fallback_when_main_queries_find_offers() -> None:
    fn = _Scripted([_offer(1)], [_offer(2)])

    asyncio.run(search_products("rtx", 1000.0, search_fn=fn))

    assert fn.queries == build_queries("rtx")


# --- Rotación de identidad en httpx ----------------------------------------------


def _capture_headers(urls: list[tuple[str, str | None]]) -> list[httpx.Headers]:
    sent: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        return httpx.Response(200)

    async def run() -> None:
        async with create_client(httpx.MockTransport(handler)) as client:
            for url, referer in urls:
                headers = {"Referer": referer} if referer else None
                await client.get(url, headers=headers)

    asyncio.run(run())
    return sent


def test_client_rotates_identity_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    agents = iter([CHROME_WIN, EDGE_MAC])
    monkeypatch.setattr(scraper, "random_user_agent", lambda: next(agents))

    a1, b, a2 = _capture_headers(
        [
            ("https://a.test/1", None),
            ("https://b.test/1", None),
            ("https://a.test/2", None),
        ]
    )

    assert a1["User-Agent"] == a2["User-Agent"] == CHROME_WIN  # estable por sitio
    assert b["User-Agent"] == EDGE_MAC
    assert '"Microsoft Edge"' in b["Sec-Ch-Ua"]
    assert b["Sec-Ch-Ua-Platform"] == '"macOS"'
    assert a1["Accept-Language"] in scraper.ACCEPT_LANGUAGES
    assert "python-httpx" not in a1["User-Agent"]


def test_client_sends_realistic_sec_fetch_site() -> None:
    direct, same, cross = _capture_headers(
        [
            ("https://a.test/1", None),
            ("https://a.test/2", "https://a.test/1"),
            ("https://b.test/1", "https://duckduckgo.com/"),
        ]
    )

    assert direct["Sec-Fetch-Site"] == "none"
    assert same["Sec-Fetch-Site"] == "same-origin"
    assert cross["Sec-Fetch-Site"] == "cross-site"
    assert direct["Sec-Fetch-Mode"] == "navigate"
    assert direct["Accept"].startswith("text/html")


def test_identity_is_kept_across_redirects() -> None:
    sent: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers)
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "https://a.test/new"})
        return httpx.Response(200)

    async def run() -> None:
        async with create_client(httpx.MockTransport(handler)) as client:
            await client.get("https://a.test/old")

    asyncio.run(run())

    assert len(sent) == 2
    assert sent[0]["User-Agent"] == sent[1]["User-Agent"]


# --- API de búsqueda de pago (fallback) -----------------------------------------

from scraper import (  # noqa: E402
    SearchApiConfig,
    api_search,
    describe_api_error,
    is_blocked,
    search_api_config,
)

API_KEY = "test-api-key-123"
EX = scraper.SITE_EXCLUSIONS  # exclusiones -site: de toda consulta a la API

SERPER_RESPONSE = {
    "organic": [
        {
            "title": "MSI RTX 4070 Ventus | PcTienda",
            "link": "https://pctienda.es/msi-rtx-4070",
            "snippet": "Tarjeta gráfica MSI. En stock.",
            "price": 649.0,
            "currency": "€",
        },
        {
            "title": "RTX 4070 Eagle - Otra",
            "link": "https://otra.es/eagle",
            "snippet": "Ahora 599,90 € envío gratis",
        },
    ]
}
SERPAPI_RESPONSE = {
    "organic_results": [
        {
            "title": "RTX 4070 Dual",
            "link": "https://serp.es/dual",
            "snippet": "Gráfica ASUS Dual.",
            "rich_snippet": {
                "top": {
                    "detected_extensions": {"price": 579.0, "currency": "€"},
                    "extensions": ["579,00 €", "Agotado"],
                }
            },
        }
    ]
}
GOOGLE_RESPONSE = {
    "items": [
        {
            "title": "RTX 4070 Gaming OC",
            "link": "https://google-shop.es/oc",
            "snippet": "Gigabyte Gaming OC 12 GB.",
            "pagemap": {
                "offer": [
                    {
                        "price": "629.00",
                        "pricecurrency": "EUR",
                        "availability": "https://schema.org/InStock",
                    }
                ]
            },
        }
    ]
}


def _api_client(
    response: httpx.Response | Exception, requests: list[httpx.Request] | None = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _enable_api(
    monkeypatch: pytest.MonkeyPatch, provider: str = "serper", **extra: str
) -> None:
    monkeypatch.setenv("SEARCH_API_KEY", API_KEY)
    monkeypatch.setenv("SEARCH_API_PROVIDER", provider)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def _search_with_api(
    search_fn: scraper.SearchFn,
    response: httpx.Response | Exception,
    requests: list[httpx.Request] | None = None,
    max_price: float = 1000.0,
) -> list[Product]:
    async def run() -> list[Product]:
        async with _api_client(response, requests) as client:
            return await search_products(
                "rtx 4070", max_price, search_fn=search_fn, api_client=client
            )

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"SEARCH_API_KEY": "  "}, None),
        ({"SEARCH_API_KEY": "k"}, SearchApiConfig("serper", "k")),
        (
            {"SEARCH_API_KEY": "k", "SEARCH_API_PROVIDER": "SerpAPI"},
            SearchApiConfig("serpapi", "k"),
        ),
        (
            {
                "SEARCH_API_KEY": "k",
                "SEARCH_API_PROVIDER": "google",
                "GOOGLE_CSE_ID": "cx1",
            },
            SearchApiConfig("google", "k", "cx1"),
        ),
        ({"SEARCH_API_KEY": "k", "SEARCH_API_PROVIDER": "google"}, None),
        ({"SEARCH_API_KEY": "k", "SEARCH_API_PROVIDER": "bing"}, None),
    ],
)
def test_search_api_config(
    env: dict[str, str], expected: SearchApiConfig | None
) -> None:
    assert search_api_config(env) == expected


def test_search_api_config_never_shows_the_key() -> None:
    assert API_KEY not in repr(SearchApiConfig("serper", API_KEY))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RatelimitException("x"), True),
        (DDGSException("https://html.duckduckgo.com 403 Forbidden"), True),
        (DDGSException("captcha required"), True),
        (RuntimeError("If this error persists, please let us know: anomaly"), True),
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "https://a.test"),
                response=httpx.Response(403),
            ),
            True,
        ),
        (OSError("[Errno 11001] getaddrinfo failed"), False),
        (RuntimeError("algo raro"), False),
    ],
)
def test_is_blocked(error: Exception, expected: bool) -> None:
    assert is_blocked(error) is expected


def test_serper_request_is_a_post_with_header_key() -> None:
    requests: list[httpx.Request] = []

    async def run() -> list[dict[str, str]]:
        response = httpx.Response(200, json=SERPER_RESPONSE)
        async with _api_client(response, requests) as client:
            return await api_search(
                "rtx 4070", SearchApiConfig("serper", API_KEY), client=client
            )

    results = asyncio.run(run())

    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == scraper.SERPER_URL
    assert request.headers["X-API-KEY"] == API_KEY
    import json

    assert json.loads(request.content) == {
        "q": "rtx 4070",
        "gl": "es",
        "hl": "es",
        "num": scraper.API_NUM_RESULTS,
    }
    assert results[0]["href"] == "https://pctienda.es/msi-rtx-4070"
    assert "649.0 EUR" in results[0]["body"]


@pytest.mark.parametrize(
    ("config", "response", "url", "product"),
    [
        (
            SearchApiConfig("serper", API_KEY),
            SERPER_RESPONSE,
            "https://pctienda.es/msi-rtx-4070",
            Product(
                "MSI RTX 4070 Ventus",
                649.0,
                True,
                "pctienda.es",
                "https://pctienda.es/msi-rtx-4070",
            ),
        ),
        (
            SearchApiConfig("serpapi", API_KEY),
            SERPAPI_RESPONSE,
            "https://serp.es/dual",
            Product("RTX 4070 Dual", 579.0, False, "serp.es", "https://serp.es/dual"),
        ),
        (
            SearchApiConfig("google", API_KEY, "cx1"),
            GOOGLE_RESPONSE,
            "https://google-shop.es/oc",
            Product(
                "RTX 4070 Gaming OC",
                629.0,
                True,
                "google-shop.es",
                "https://google-shop.es/oc",
            ),
        ),
    ],
)
def test_api_results_become_products(
    config: SearchApiConfig, response: dict, url: str, product: Product
) -> None:
    async def run() -> list[dict[str, str]]:
        async with _api_client(httpx.Response(200, json=response)) as client:
            return await api_search("rtx 4070", config, client=client)

    results = asyncio.run(run())
    parsed = {r["href"]: parse_web_result(r) for r in results}

    assert parsed[url] == product


def test_serpapi_and_google_send_key_as_query_param() -> None:
    for config, base in (
        (SearchApiConfig("serpapi", API_KEY), scraper.SERPAPI_URL),
        (SearchApiConfig("google", API_KEY, "cx1"), scraper.GOOGLE_CSE_URL),
    ):
        request = scraper._api_request("rtx 4070", config)
        assert str(request.url).startswith(base)
        assert API_KEY in request.url.params.values()
        assert request.url.params["q"] == "rtx 4070"


def json_query(request: httpx.Request) -> str:
    import json

    return json.loads(request.content)["q"]


def test_api_is_the_primary_engine_when_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    fn = _Scripted([_offer(1)])  # ddgs funcionaría, pero no se usa
    requests: list[httpx.Request] = []

    products = _search_with_api(fn, httpx.Response(200, json=SERPER_RESPONSE), requests)

    # 649 € va primero por estar "En stock"; 599,90 € tiene stock sin confirmar.
    assert [p.price for p in products] == [649.0, 599.9]
    assert len(requests) == 1  # una sola llamada: no gasta cuota de más
    assert json_query(requests[0]) == f"rtx 4070 1..1000 € {EX}"
    assert fn.queries == []


def test_without_api_key_ddgs_is_used_and_api_never_called() -> None:
    fn = _Scripted([_offer(1)])
    requests: list[httpx.Request] = []

    products = _search_with_api(fn, httpx.Response(200, json=SERPER_RESPONSE), requests)

    assert [p.store_name for p in products] == ["tienda1.es"]
    assert requests == []


def test_without_api_key_rate_limit_still_raises_search_error() -> None:
    fn = _Scripted(*[RatelimitException("x")] * SEARCH_ATTEMPTS)
    requests: list[httpx.Request] = []

    with pytest.raises(SearchError, match="limitado"):
        _search_with_api(fn, httpx.Response(200, json=SERPER_RESPONSE), requests)

    assert requests == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"message": f"Unauthorized {API_KEY}"}),
        httpx.Response(403),
        httpx.Response(429),
        httpx.Response(500),
        httpx.Response(200, text="<html>no es json</html>"),
        httpx.ConnectError("boom"),
        httpx.ReadTimeout("timeout"),
    ],
)
def test_api_failure_falls_back_to_ddgs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response | Exception,
) -> None:
    _enable_api(monkeypatch, "serpapi")  # la clave va en la URL: no debe filtrarse
    fn = _Scripted([_offer(1)])

    with caplog.at_level(logging.INFO, logger="scraper"):
        products = _search_with_api(fn, response)

    assert [p.store_name for p in products] == ["tienda1.es"]
    assert fn.queries[0] == build_queries("rtx 4070")[0]
    assert "usando ddgs" in caplog.text
    assert API_KEY not in caplog.text


def test_api_without_results_falls_back_to_ddgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    fn = _Scripted([_offer(1)])

    products = _search_with_api(fn, httpx.Response(200, json={"organic": []}))

    assert [p.store_name for p in products] == ["tienda1.es"]
    assert fn.queries[0] == build_queries("rtx 4070")[0]


def test_api_results_with_only_pages_to_inspect_do_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un resultado sin precio en el snippet es útil (se inspecciona su página)."""
    _enable_api(monkeypatch)
    fn = _Scripted([_offer(1)])
    response = {"organic": [{"title": "RTX 4070", "link": "https://t1.es/p"}]}

    async def run() -> list[Product]:
        async with (
            _api_client(httpx.Response(200, json=response)) as api,
            _page_client({"https://t1.es/p": _html(HTML_OG)}) as pages,
        ):
            return await search_products(
                "rtx 4070", 1000.0, search_fn=fn, api_client=api, page_client=pages
            )

    products = asyncio.run(run())

    assert [(p.url, p.price) for p in products] == [("https://t1.es/p", 589.9)]
    assert fn.queries == []


def test_api_failure_and_ddgs_block_is_a_clean_search_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _enable_api(monkeypatch, "serpapi")
    fn = _Scripted(*[RatelimitException("x")] * SEARCH_ATTEMPTS)

    with (
        caplog.at_level(logging.WARNING, logger="scraper"),
        pytest.raises(SearchError, match="limitado"),
    ):
        _search_with_api(fn, httpx.Response(429))

    assert "La API de búsqueda (serpapi) falló: HTTP 429" in caplog.text
    assert "rate-limit" in caplog.text
    assert API_KEY not in caplog.text


def test_api_failure_and_ddgs_dns_error_is_a_clean_search_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    fn = _Scripted(*[OSError("[Errno 11001] getaddrinfo failed")] * SEARCH_ATTEMPTS)

    with pytest.raises(SearchError, match="DNS"):
        _search_with_api(fn, httpx.ConnectError("[Errno 11001] getaddrinfo failed"))


def test_api_failure_and_ddgs_zero_results_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)

    assert _search_with_api(_Scripted(), httpx.Response(500)) == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", f"https://serpapi.com/?api_key={API_KEY}"),
                response=httpx.Response(401),
            ),
            "HTTP 401: clave no válida",
        ),
        (
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "https://a.test"),
                response=httpx.Response(429),
            ),
            "HTTP 429: cuota o límite de peticiones agotado",
        ),
        (httpx.ReadTimeout("t"), "tiempo de espera agotado"),
        (ValueError("bad json"), "respuesta no válida (ValueError)"),
    ],
)
def test_describe_api_error_hides_url(error: Exception, expected: str) -> None:
    message = describe_api_error(error)

    assert message == expected
    assert API_KEY not in message


def test_api_price_respects_max_price(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_api(monkeypatch)
    fn = _Scripted(*[RatelimitException("x")] * SEARCH_ATTEMPTS)

    products = _search_with_api(
        fn, httpx.Response(200, json=SERPER_RESPONSE), max_price=600.0
    )

    assert [p.price for p in products] == [599.9]


# --- API: consulta avanzada con rango de precio y filtro estricto ---------------

from scraper import build_api_query, filter_by_max_price  # noqa: E402


@pytest.mark.parametrize(
    ("name", "max_price", "expected"),
    [
        ("rtx 4070", 700.0, f"rtx 4070 1..700 € {EX}"),
        ("RTX 4070", 649.99, f"RTX 4070 1..650 € {EX}"),
        ('comprar "iPhone 15" precio €', 900.0, f"iPhone 15 1..900 € {EX}"),
        ("cable", 0.5, f"cable 1..1 € {EX}"),
        # Comillas tipográficas, paréntesis, operadores y símbolos fuera.
        (
            "“MSI” RTX-4070 (Ventus 2X) -funda *oferta* 12GB!",
            700.0,
            f"MSI RTX-4070 Ventus 2X funda 12GB 1..700 € {EX}",
        ),
        (
            'SSD 2.5" 1TB <Samsung> & más',
            90.0,
            f"SSD 2.5 1TB Samsung más 1..90 € {EX}",
        ),
        (
            "usb-c 3.1 cable: 2m; ¿barato?",
            15.0,
            f"usb-c 3.1 cable 2m 1..15 € {EX}",
        ),
        ("Core i7+ 13700K", 400.0, f"Core i7+ 13700K 1..400 € {EX}"),
        ("comprar precio €", 100.0, f"comprar precio 1..100 € {EX}"),
    ],
)
def test_build_api_query_uses_clean_price_range(
    name: str, max_price: float, expected: str
) -> None:
    assert build_api_query(name, max_price) == expected


@pytest.mark.parametrize(
    "name",
    [
        'rtx "4070"',
        "“MSI” RTX-4070 (Ventus) -funda *oferta*",
        'SSD <1TB> & {más} [nuevo] | 2.5" ~ ^ = % $ # @ \\ `',
    ],
)
def test_build_api_query_has_no_quotes_or_special_characters(name: str) -> None:
    query = build_api_query(name, 700.0)

    assert re.fullmatch(r"[\w .,+\-/€]+", query.removesuffix(f" {EX}")), query
    assert "€.." not in query and "..€" not in query  # rango sin moneda dentro
    assert query.endswith(f" 1..700 € {EX}")


def test_serper_payload_targets_spain_with_price_range_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    requests: list[httpx.Request] = []

    _search_with_api(
        _Scripted(),
        httpx.Response(200, json=SERPER_RESPONSE),
        requests,
        max_price=700.0,
    )

    import json

    payload = json.loads(requests[0].content)
    assert payload["gl"] == "es"
    assert payload["hl"] == "es"
    assert payload["q"] == f"rtx 4070 1..700 € {EX}"
    assert requests[0].method == "POST"


def test_filter_by_max_price_drops_only_priced_results_over_limit() -> None:
    results = [
        {"title": "A 500 €", "href": "https://a.es/p", "body": ""},
        {"title": "B", "href": "https://b.es/p", "body": "Ahora 750 €"},
        {"title": "C sin precio", "href": "https://c.es/p", "body": "Ficha técnica"},
        {"title": "D 700 €", "href": "https://d.es/p", "body": ""},
    ]

    kept = filter_by_max_price(results, 700.0)

    assert [r["href"] for r in kept] == [
        "https://a.es/p",
        "https://c.es/p",  # sin precio: se inspeccionará su página
        "https://d.es/p",  # el límite es inclusivo
    ]


def test_api_results_over_max_price_are_never_inspected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con la API, lo que supera el límite se descarta sin descargar su página
    (a diferencia de ddgs, donde se revisa por si hay rebaja)."""
    _enable_api(monkeypatch)
    response = {
        "organic": [
            {"title": "RTX 4070 - 720 €", "link": "https://caro.es/p", "snippet": ""},
            {"title": "RTX 4070 barata", "link": "https://t1.es/p", "snippet": ""},
        ]
    }
    fetched: list[str] = []

    def pages(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return _html(HTML_OG)  # 589,90 €

    async def run() -> list[Product]:
        transport = httpx.MockTransport(pages)
        async with (
            _api_client(httpx.Response(200, json=response)) as api,
            httpx.AsyncClient(transport=transport) as page_client,
        ):
            return await search_products(
                "rtx 4070",
                700.0,
                search_fn=_Scripted(),
                api_client=api,
                page_client=page_client,
            )

    products = asyncio.run(run())

    assert [(p.url, p.price) for p in products] == [("https://t1.es/p", 589.9)]
    assert fetched == ["https://t1.es/p"]


# --- API: consulta ultra-limpia y reintento sin rango ---------------------------

from scraper import clean_api_product, search_api_with_fallback  # noqa: E402


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rtx-4070 super", "rtx-4070 super"),
        ("“MSI” RTX-4070 [Ventus] *oferta* comprar", "MSI RTX-4070 Ventus"),
        ('"iPhone 15 Pro" 256GB precio €', "iPhone 15 Pro 256GB"),
    ],
)
def test_clean_api_product(name: str, expected: str) -> None:
    assert clean_api_product(name) == expected


def test_api_query_has_no_purchase_terms() -> None:
    assert build_api_query("comprar rtx 4070 oferta tienda barato", 700.0) == (
        f"rtx 4070 1..700 € {EX}"
    )


def _scripted_api(*responses: httpx.Response) -> tuple[httpx.AsyncClient, list[str]]:
    """Cliente cuya respuesta n-ésima es ``responses[n]``; anota cada ``q``."""
    queue = list(responses)
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(json_query(request))
        return queue.pop(0)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), queries


def _run_fallback(*responses: httpx.Response, max_price: float = 700.0) -> tuple:
    client, queries = _scripted_api(*responses)

    async def run() -> list[dict[str, str]]:
        async with client:
            return await search_api_with_fallback(
                "Comprar “RTX-4070” super",
                max_price,
                SearchApiConfig("serper", API_KEY),
                client=client,
            )

    return asyncio.run(run()), queries


def test_range_query_with_results_needs_a_single_call() -> None:
    results, queries = _run_fallback(httpx.Response(200, json=SERPER_RESPONSE))

    assert queries == [f"RTX-4070 super 1..700 € {EX}"]
    assert [r["href"] for r in results] == [
        "https://pctienda.es/msi-rtx-4070",
        "https://otra.es/eagle",
    ]


@pytest.mark.parametrize(
    "first",
    [
        httpx.Response(400, json={"message": "Query not allowed"}),
        httpx.Response(200, json={"organic": []}),
        httpx.Response(200, json={}),
    ],
)
def test_400_or_zero_results_retries_with_product_only(first: httpx.Response) -> None:
    results, queries = _run_fallback(first, httpx.Response(200, json=SERPER_RESPONSE))

    assert queries == [f"RTX-4070 super 1..700 € {EX}", f"RTX-4070 super {EX}"]
    assert len(results) == 2


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_other_api_errors_do_not_retry_without_range(status: int) -> None:
    client, queries = _scripted_api(httpx.Response(status))

    async def run() -> None:
        async with client:
            await search_api_with_fallback(
                "rtx 4070", 700.0, SearchApiConfig("serper", API_KEY), client=client
            )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run())
    assert queries == [f"rtx 4070 1..700 € {EX}"]


def test_retry_failure_propagates_and_search_falls_back_to_ddgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    client, queries = _scripted_api(httpx.Response(400), httpx.Response(400))

    async def run() -> list[Product]:
        async with client:
            return await search_products(
                "rtx 4070", 1000.0, search_fn=_Scripted([_offer(1)]), api_client=client
            )

    products = asyncio.run(run())

    assert queries == [f"rtx 4070 1..1000 € {EX}", f"rtx 4070 {EX}"]
    assert [p.store_name for p in products] == ["tienda1.es"]


def test_retry_without_range_is_still_filtered_by_max_price() -> None:
    """Sin el operador de rango Google devuelve de todo: el filtro lo recorta."""
    unfiltered = {
        "organic": [
            {"title": "RTX 4070 - 549 €", "link": "https://a.es/p", "snippet": ""},
            {"title": "RTX 4070 Ti - 899 €", "link": "https://b.es/p", "snippet": ""},
            {"title": "RTX 4070 ficha", "link": "https://c.es/p", "snippet": ""},
        ]
    }

    results, _ = _run_fallback(
        httpx.Response(200, json={"organic": []}),
        httpx.Response(200, json=unfiltered),
        max_price=600.0,
    )

    assert [r["href"] for r in results] == ["https://a.es/p", "https://c.es/p"]


def test_zero_results_in_both_calls_falls_back_to_ddgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    empty = {"organic": []}
    client, queries = _scripted_api(
        httpx.Response(200, json=empty), httpx.Response(200, json=empty)
    )

    async def run() -> list[Product]:
        async with client:
            return await search_products(
                "rtx 4070", 1000.0, search_fn=_Scripted([_offer(1)]), api_client=client
            )

    products = asyncio.run(run())

    assert len(queries) == 2
    assert [p.store_name for p in products] == ["tienda1.es"]


def test_retry_payload_keeps_spanish_localisation() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(400)
        return httpx.Response(200, json=SERPER_RESPONSE)

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await search_api_with_fallback(
                "rtx 4070", 700.0, SearchApiConfig("serper", API_KEY), client=client
            )

    asyncio.run(run())

    import json

    payloads = [json.loads(r.content) for r in sent]
    assert [p["q"] for p in payloads] == [f"rtx 4070 1..700 € {EX}", f"rtx 4070 {EX}"]
    assert all(p["gl"] == "es" and p["hl"] == "es" for p in payloads)


# --- Solo tiendas: lista negra de dominios ------------------------------------

from scraper import (  # noqa: E402
    BLOCKED_DOMAINS,
    EXCLUDED_SITES,
    build_api_fallback_query,
    filter_store_results,
    is_valid_store_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.pccomponentes.com/msi-geforce-rtx-4070-ventus-2x",
        "https://www.amazon.es/dp/B0BZ8Z1234",
        "https://www.coolmod.com/msi-rtx-4070/",
        "https://www.mediamarkt.es/es/product/_rtx-4070.html",
        "http://tienda.example.es/p?id=1",
        "https://www.box.com/p",  # termina en "x.com" pero no es x.com
        "https://youtubers-shop.es/p",  # contiene "youtube" sin ser youtube.com
        "https://WWW.Coolmod.COM./p",  # mayúsculas y punto final
    ],
)
def test_is_valid_store_url_accepts_shops(url: str) -> None:
    assert is_valid_store_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=xxx",
        "https://m.youtube.com/watch?v=xxx",
        "https://youtu.be/xxx",
        "https://www.reddit.com/r/nvidia/comments/abc/rtx_4070/",
        "https://old.reddit.com/r/buildapc",
        "https://es.wikipedia.org/wiki/GeForce_40",
        "https://www.tiktok.com/@user/video/1",
        "https://www.instagram.com/p/xyz",
        "https://m.facebook.com/marketplace/item/1",
        "https://twitter.com/user/status/1",
        "https://x.com/user/status/1",
        "https://www.pinterest.com/pin/1",
        "https://vimeo.com/123",
        "https://www.twitch.tv/canal",
        "https://es.wikihow.com/elegir-una-tarjeta-grafica",
        "https://medium.com/@autor/rtx-4070-review",
        "https://autor.medium.com/rtx-4070",
        # URLs no utilizables
        "ftp://amazon.es/p",
        "amazon.es/p",
        "",
        "https://[::1/p",
    ],
)
def test_is_valid_store_url_rejects_non_shops(url: str) -> None:
    assert is_valid_store_url(url) is False


def test_blocked_domains_cover_the_requested_platforms() -> None:
    assert {
        "youtube.com", "youtu.be", "tiktok.com", "instagram.com", "facebook.com",
        "twitter.com", "x.com", "pinterest.com", "vimeo.com", "twitch.tv",
        "reddit.com", "wikipedia.org", "wikihow.com", "medium.com",
    } <= BLOCKED_DOMAINS  # fmt: skip


EXPECTED_EXCLUSIONS = (
    "-site:youtube.com -site:youtu.be -site:tiktok.com -site:instagram.com "
    "-site:facebook.com -site:twitter.com -site:x.com -site:pinterest.com "
    "-site:reddit.com -site:vimeo.com -site:twitch.tv -site:wikipedia.org "
    "-site:wikihow.com -site:medium.com"
)


def test_api_queries_exclude_social_and_video_sites() -> None:
    assert EX == EXPECTED_EXCLUSIONS
    assert build_api_query("rtx 4070", 700.0) == (
        f"rtx 4070 1..700 € {EXPECTED_EXCLUSIONS}"
    )
    assert build_api_fallback_query("rtx 4070") == f"rtx 4070 {EXPECTED_EXCLUSIONS}"
    assert len(EXCLUDED_SITES) == 14


def test_every_blocked_domain_is_also_excluded_in_the_api_query() -> None:
    """Lo que se descarta en Python ni se pide a Serper (no gasta resultados)."""
    assert set(EXCLUDED_SITES) == BLOCKED_DOMAINS
    assert len(EXCLUDED_SITES) == len(set(EXCLUDED_SITES))  # sin duplicados


def test_both_api_calls_send_the_site_exclusions() -> None:
    """Tanto la búsqueda con rango como el reintento sin rango llevan -site:."""
    _, queries = _run_fallback(
        httpx.Response(400), httpx.Response(200, json=SERPER_RESPONSE)
    )

    assert len(queries) == 2
    assert all(q.endswith(EXPECTED_EXCLUSIONS) for q in queries)
    assert "1..700 €" in queries[0] and "1..700" not in queries[1]


def test_filter_store_results_keeps_order_and_drops_blocked() -> None:
    results = [
        {"title": "A", "href": "https://www.youtube.com/watch?v=1"},
        {"title": "B", "href": "https://www.pccomponentes.com/p"},
        {"title": "C", "url": "https://es.wikipedia.org/wiki/X"},
        {"title": "D", "url": "https://www.amazon.es/dp/1"},
        {"title": "E", "href": "no-es-una-url"},
    ]

    kept = filter_store_results(results)

    assert [r["title"] for r in kept] == ["B", "D"]


SOCIAL_ONLY = {
    "organic": [
        {"title": "RTX 4070 review 599 €", "link": "https://www.youtube.com/watch?v=1"},
        {"title": "RTX 4070 vale la pena", "link": "https://www.reddit.com/r/x/1"},
    ]
}


def test_api_non_store_results_are_dropped_before_price_filter() -> None:
    mixed = {"organic": SOCIAL_ONLY["organic"] + SERPER_RESPONSE["organic"]}

    results, queries = _run_fallback(httpx.Response(200, json=mixed))

    assert len(queries) == 1
    assert [r["href"] for r in results] == [
        "https://pctienda.es/msi-rtx-4070",
        "https://otra.es/eagle",
    ]


def test_only_non_store_api_results_trigger_retry_without_range() -> None:
    results, queries = _run_fallback(
        httpx.Response(200, json=SOCIAL_ONLY),
        httpx.Response(200, json=SERPER_RESPONSE),
    )

    assert len(queries) == 2
    assert len(results) == 2


def test_api_with_only_non_store_results_falls_back_to_ddgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_api(monkeypatch)
    client, queries = _scripted_api(
        httpx.Response(200, json=SOCIAL_ONLY), httpx.Response(200, json=SOCIAL_ONLY)
    )

    async def run() -> list[Product]:
        async with client:
            return await search_products(
                "rtx 4070", 1000.0, search_fn=_Scripted([_offer(1)]), api_client=client
            )

    products = asyncio.run(run())

    assert len(queries) == 2
    assert [p.store_name for p in products] == ["tienda1.es"]


def test_api_empty_after_domain_and_price_filters_returns_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin nada útil en la API ni en ddgs: lista vacía, sin excepciones."""
    _enable_api(monkeypatch)
    expensive = {
        "organic": [
            {"title": "RTX 4070 - 900 €", "link": "https://tienda.es/p"},
            {"title": "RTX 4070 - 99 €", "link": "https://www.youtube.com/watch"},
        ]
    }
    client, _ = _scripted_api(
        httpx.Response(200, json=expensive), httpx.Response(200, json=expensive)
    )

    async def run() -> list[Product]:
        async with client:
            return await search_products(
                "rtx 4070", 500.0, search_fn=_Scripted(), api_client=client
            )

    assert asyncio.run(run()) == []


def test_ddgs_results_from_blocked_domains_are_discarded() -> None:
    results = [
        {
            "title": "RTX 4070 unboxing 549 €",
            "href": "https://www.youtube.com/watch?v=abc",
            "body": "Vídeo",
        },
        {
            "title": "Opiniones RTX 4070 - 499 €",
            "href": "https://www.reddit.com/r/nvidia/comments/1",
            "body": "",
        },
        {
            "title": "RTX 4070 sin precio",
            "href": "https://es.wikipedia.org/wiki/RTX_4070",
            "body": "",
        },
        _offer(1),
    ]
    fetched: list[str] = []

    def pages(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(404)

    async def run() -> list[Product]:
        transport = httpx.MockTransport(pages)
        async with httpx.AsyncClient(transport=transport) as page_client:
            return await search_products(
                "rtx 4070",
                1000.0,
                search_fn=lambda _: results,
                page_client=page_client,
            )

    products = asyncio.run(run())

    assert [p.store_name for p in products] == ["tienda1.es"]
    assert not any("wikipedia" in url for url in fetched)  # ni se inspecciona
