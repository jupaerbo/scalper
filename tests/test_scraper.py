import asyncio
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
