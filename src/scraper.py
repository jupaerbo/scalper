"""Búsqueda global de productos en internet (DuckDuckGo).

No depende de tiendas fijas: busca en la web y extrae de cada resultado el
título, el precio, el dominio y el stock a partir del título y del fragmento.
"""

import asyncio
import functools
import html as html_lib
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from functools import cache
from urllib.parse import urlsplit

import httpx
from fake_useragent import UserAgent

from models import Product

logger = logging.getLogger(__name__)

# --- Búsqueda web ------------------------------------------------------------

WEB_REGION = "es-es"
WEB_MAX_RESULTS = 50
MIN_WEB_PRICE = 1.0
MIN_PRODUCTS = 10
MAX_PRODUCTS = 15
# Un precio del fragmento hasta un 50 % por encima del límite se comprueba en
# la página, por si la oferta actual sí entra en el presupuesto.
RECHECK_PRICE_MARGIN = 1.5

PURCHASE_TERMS = (
    "comprar",
    "compra",
    "precio",
    "precios",
    "oferta",
    "ofertas",
    "barato",
    "tienda",
    "€",
    "eur",
    "euros",
    "buy",
    "price",
    "deal",
    "shop",
)
# Sufijos que se prueban por orden si el usuario no escribió términos de compra.
PURCHASE_QUERY_VARIANTS = ("comprar precio €", "oferta tienda online €")

OUT_OF_STOCK_KEYWORDS = (
    "agotado",
    "temporalmente agotado",
    "sin stock",
    "fuera de stock",
    "no hay stock",
    "no disponible",
    "no está disponible",
    "sin fecha de entrada",
    "descatalogado",
    "recibir aviso cuando",
    "recibir aviso de disponibilidad",
    "avísame cuando",
    "avisarme cuando",
    "out of stock",
    "sold out",
    "currently unavailable",
    "unavailable",
    "discontinued",
    "notify me when",
)
# "Quedan pocas" implica stock aunque contenga "agotado" ("casi agotado").
LOW_STOCK_KEYWORDS = (
    "casi agotado",
    "a punto de agotarse",
    "últimas unidades",
    "pocas unidades",
    "only a few left",
)
IN_STOCK_KEYWORDS = (
    "en stock",
    "disponible",
    "envío inmediato",
    "entrega mañana",
    "in stock",
)


def _keyword_re(keywords: tuple[str, ...]) -> re.Pattern[str]:
    """Regex que busca cualquiera de ``keywords`` como palabras completas."""
    alternatives = "|".join(re.escape(k) for k in sorted(keywords, key=len)[::-1])
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")


_OUT_OF_STOCK_RE = _keyword_re(OUT_OF_STOCK_KEYWORDS)
_LOW_STOCK_RE = _keyword_re(LOW_STOCK_KEYWORDS)
_IN_STOCK_RE = _keyword_re(IN_STOCK_KEYWORDS)

# Palabras justo antes de un importe que lo marcan como precio anterior
# (tachado) o como precio rebajado/actual.
_OLD_PRICE_HINT = re.compile(
    r"(?<!\w)(?:antes|precio anterior|precio original|precio habitual|"
    r"pvpr|pvp recomendado|precio recomendado|was|before|rrp|msrp|list price)"
    r"(?!\w)"
)
_SALE_PRICE_HINT = re.compile(
    r"(?<!\w)(?:ahora|oferta|rebajad[oa]|precio final|precio rebajado|"
    r"por solo|solo|desde|now|sale|deal)(?!\w)"
)

_NUMBER_RE = re.compile(r"\d(?:[\d.,]*\d)?")
_AMOUNT = r"(?<![\d.,])(?:\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
# (patrón, moneda) en orden de preferencia: euros, dólares y "desde N" (€).
_PRICE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"(?:€|\beur\b)\s?({_AMOUNT})", re.IGNORECASE), "EUR"),
    (re.compile(rf"({_AMOUNT})\s?(?:€|\beur(?:os?)?\b)", re.IGNORECASE), "EUR"),
    (re.compile(rf"(?:US\$|\$|\busd\b)\s?({_AMOUNT})", re.IGNORECASE), "USD"),
    (
        re.compile(rf"({_AMOUNT})\s?(?:\$|\busd\b|d[oó]lares)", re.IGNORECASE),
        "USD",
    ),
    (re.compile(rf"\b(?:desde|por solo|por)\s+({_AMOUNT})\b", re.IGNORECASE), "EUR"),
)
_PER_UNIT = re.compile(
    r"\s?(?:€|\$|eur(?:os?)?|usd)?\s?(?:/|al |por |a la |per )\s?"
    r"(?:h\b|hora|mes|month|mo\b|día|dia|day|año|year|semana|week|ud|unidad)",
    re.IGNORECASE,
)
_TITLE_SEPARATORS = re.compile(r"\s+[|\-–—»·]\s+")

SearchFn = Callable[[str], list[dict[str, str]]]


class SearchError(Exception):
    """La búsqueda web falló. El mensaje es apto para mostrar al usuario."""


def parse_price(text: str) -> float | None:
    """Convierte un texto de precio en ``float``.

    Admite separadores europeos (``1.299,99 €``) y anglosajones (``$1,299.99``).
    Un único separador seguido de exactamente 3 cifras se toma como de miles
    (``1.299`` → 1299), que es lo habitual en precios.

    Returns:
        El precio, o ``None`` si no se encuentra ningún número.
    """
    match = _NUMBER_RE.search(text)
    if match is None:
        return None
    raw = match.group()
    if "," in raw and "." in raw:
        decimal = "," if raw.rfind(",") > raw.rfind(".") else "."
    elif "," in raw:
        decimal = "," if len(raw.rpartition(",")[2]) <= 2 else ""
    else:
        single = raw.count(".") == 1 and len(raw.rpartition(".")[2]) != 3
        decimal = "." if single else ""
    for sep in {",", "."} - {decimal}:
        raw = raw.replace(sep, "")
    return float(raw.replace(",", "."))


@dataclass(frozen=True, slots=True)
class _FoundPrice:
    price: float
    currency: str
    on_sale: bool


def _find_price(text: str) -> _FoundPrice | None:
    """Precio más relevante de ``text``: el rebajado si lo hay, si no el primero.

    Descarta los importes precedidos de "antes", "PVPR", "was"… (precio
    tachado) y prefiere los precedidos de "ahora", "oferta", "now"…
    """
    first: _FoundPrice | None = None
    for pattern, currency in _PRICE_PATTERNS:
        for match in pattern.finditer(text):
            if _PER_UNIT.match(text, match.end()):
                continue
            price = parse_price(match.group(1))
            if price is None or price < MIN_WEB_PRICE:
                continue
            # Solo cuenta lo escrito tras el importe anterior: en "antes 799 €
            # ahora 649 €" el contexto de 649 es " € ahora ".
            window = text[max(0, match.start() - 30) : match.start()]
            context = re.split(r"\d", window)[-1].casefold()
            hints = [
                (m.start(), on_sale)
                for regex, on_sale in (
                    (_OLD_PRICE_HINT, False),
                    (_SALE_PRICE_HINT, True),
                )
                for m in regex.finditer(context)
            ]
            if hints:
                on_sale = max(hints)[1]
                if not on_sale:
                    continue  # precio anterior/tachado
                return _FoundPrice(price, currency, on_sale=True)
            first = first or _FoundPrice(price, currency, on_sale=False)
    return first


def extract_price_and_currency(text: str) -> tuple[float, str] | None:
    """Busca un precio y su moneda (``EUR`` o ``USD``) en un texto libre.

    Reconoce ``589,90 €``, ``€1100``, ``1.299 EUR``, ``99 euros``, ``$499.99``,
    ``499 USD`` y, como último recurso, ``desde 1.800,00`` (en euros). Ignora
    tarifas por unidad de tiempo (``0,09 €/hora``), importes menores de
    ``MIN_WEB_PRICE`` y precios anteriores (``antes 799 €``). Si hay un precio
    de oferta (``ahora 649 €``) lo prefiere; si no, prefiere euros.

    Returns:
        ``(precio, moneda)``, o ``None``.
    """
    found = _find_price(text)
    return (found.price, found.currency) if found else None


def extract_price(text: str) -> float | None:
    """Como ``extract_price_and_currency`` pero devuelve solo el importe."""
    found = extract_price_and_currency(text)
    return found[0] if found else None


def detect_stock(text: str) -> bool | None:
    """``False`` si indica agotado, ``True`` si indica stock; si no, ``None``.

    Busca palabras completas (``disponibilidad`` no cuenta como
    ``disponible``) y trata "casi agotado"/"últimas unidades" como en stock.
    """
    lowered = text.casefold()
    without_low = _LOW_STOCK_RE.sub(" ", lowered)
    if _OUT_OF_STOCK_RE.search(without_low):
        return False
    if without_low != lowered or _IN_STOCK_RE.search(lowered):
        return True
    return None


def extract_store(url: str) -> str:
    """Dominio sin ``www.``: ``https://www.amazon.es/x`` → ``amazon.es``."""
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def clean_title(title: str) -> str:
    """Quita del título la parte del sitio: ``RTX 4070 - Tienda`` → ``RTX 4070``."""
    first = _TITLE_SEPARATORS.split(title.strip(), maxsplit=1)[0].strip()
    return first or title.strip()


def parse_web_result(result: Mapping[str, str]) -> Product | None:
    """Convierte un resultado ``{title, href, body}`` en ``Product``.

    Returns:
        El producto, o ``None`` si no tiene URL, título o precio reconocible.
    """
    url = _result_url(result)
    title = result.get("title") or ""
    if url is None or not title:
        return None
    # El título es más fiable: el fragmento a veces mezcla texto de otros
    # resultados (p. ej. "precio oficial 669€" de otra web). Solo se prefiere
    # el del fragmento si está marcado como rebaja y es menor.
    in_title = _find_price(title)
    in_body = _find_price(result.get("body") or "")
    found = in_title or in_body
    if (
        in_title
        and in_body
        and in_body.on_sale
        and in_body.currency == in_title.currency
        and in_body.price < in_title.price
    ):
        found = in_body
    if found is None:
        return None
    return _product_from_result(result, url, found.price, found.currency)


def _product_from_result(
    result: Mapping[str, str],
    url: str,
    price: float,
    currency: str,
    available: bool | None = None,
) -> Product:
    """``Product`` a partir de un resultado de búsqueda y un precio ya hallado.

    Si ``available`` es ``None`` se deduce del título y el fragmento.
    """
    title = result.get("title") or ""
    if available is None:
        available = detect_stock(f"{title} {result.get('body') or ''}")
    return Product(
        title=clean_title(title),
        price=price,
        available=available,
        store_name=extract_store(url),
        url=url,
        currency=currency,
    )


# --- Reintentos frente a rate-limit ---

# Intentos por consulta si el buscador falla (red, rate-limit…) y si devuelve
# 0 resultados (desde IPs de servidores suele ser un bloqueo encubierto).
SEARCH_ATTEMPTS = 3
EMPTY_SEARCH_ATTEMPTS = 2
BACKOFF_BASE = 1.0  # segundos; se dobla en cada reintento
BACKOFF_MAX = 8.0
# Motores de ddgs por intento: si "auto" está bloqueado se prueban otros.
SEARCH_BACKENDS = ("auto", "duckduckgo,brave,mojeek", "yahoo,startpage,mojeek")
_RATE_LIMIT_MARKERS = ("ratelimit", "rate limit", "too many requests", "429")


def _ddg_text(query: str, backend: str = "auto") -> list[dict[str, str]]:
    from ddgs import DDGS

    # DDGS lee el proxy de la variable de entorno DDGS_PROXY si está definida.
    return DDGS().text(
        query, region=WEB_REGION, max_results=WEB_MAX_RESULTS, backend=backend
    )


def backoff_delay(retry: int) -> float:
    """Espera antes del reintento ``retry`` (0, 1, 2…): backoff exponencial
    ``BACKOFF_BASE * 2**retry`` (máx. ``BACKOFF_MAX``) con jitter aleatorio
    entre la mitad y el total, para no reintentar todos a la vez."""
    ceiling = min(BACKOFF_MAX, BACKOFF_BASE * 2**retry)
    return random.uniform(ceiling / 2, ceiling)


def is_rate_limit(exc: BaseException) -> bool:
    """``True`` si el error es un límite de peticiones del buscador."""
    from ddgs.exceptions import RatelimitException

    if isinstance(exc, RatelimitException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429
    return any(m in str(exc).casefold() for m in _RATE_LIMIT_MARKERS)


async def run_search(query: str, search_fn: SearchFn | None = None) -> list[dict]:
    """Lanza ``query`` con reintentos y backoff exponencial.

    Reintenta hasta ``SEARCH_ATTEMPTS`` veces si hay un error (red, timeout,
    rate-limit) y hasta ``EMPTY_SEARCH_ATTEMPTS`` si no hay resultados. Con el
    buscador por defecto cada intento usa otros motores (``SEARCH_BACKENDS``).

    Returns:
        Los resultados, o ``[]`` si tras los intentos no hay ninguno.

    Raises:
        Exception: El error del último intento, si el último intento falló.
    """
    for attempt in range(SEARCH_ATTEMPTS):
        backend = SEARCH_BACKENDS[min(attempt, len(SEARCH_BACKENDS) - 1)]
        fn = search_fn or functools.partial(_ddg_text, backend=backend)
        error: Exception | None = None
        try:
            results = await asyncio.to_thread(fn, query)
        except Exception as exc:
            results = []
            if not _is_no_results(exc):
                error = exc
        if results:
            return results

        last = attempt + 1 >= (SEARCH_ATTEMPTS if error else EMPTY_SEARCH_ATTEMPTS)
        if last:
            if error is None:
                return []
            if is_rate_limit(error):
                logger.warning(
                    "El buscador sigue limitando las peticiones (rate-limit) "
                    "tras %d intentos para %r. Es habitual desde IPs de "
                    "servidores compartidos (p. ej. Render); prueba a definir "
                    "DDGS_PROXY o espera unos minutos.",
                    attempt + 1,
                    query,
                )
            raise error

        delay = backoff_delay(attempt)
        if error is None:
            reason = "sin resultados"
        elif is_rate_limit(error):
            reason = "límite de peticiones (rate-limit)"
        else:
            reason = f"error {type(error).__name__}"
        logger.log(
            logging.INFO if error is None else logging.WARNING,
            "Búsqueda %r: %s; reintento %d/%d en %.1f s",
            query,
            reason,
            attempt + 1,
            (SEARCH_ATTEMPTS if error else EMPTY_SEARCH_ATTEMPTS) - 1,
            delay,
        )
        await asyncio.sleep(delay)
    return []  # pragma: no cover - el bucle siempre retorna o lanza


def describe_search_error(exc: Exception) -> str:
    """Mensaje claro para fallos del buscador (DNS, límite de peticiones…)."""
    from ddgs.exceptions import TimeoutException

    if is_rate_limit(exc):
        return "el buscador ha limitado las peticiones; espera unos minutos"
    if isinstance(exc, httpx.HTTPError):
        return describe_network_error(exc)
    if isinstance(exc, TimeoutException):
        return "el buscador tardó demasiado en responder"
    reason = str(exc).lower()
    if any(m in reason for m in ("getaddrinfo", "11001", "dns", "name resolution")):
        return "no se pudo resolver el dominio (fallo de DNS o sin internet)"
    if any(m in reason for m in ("connect", "network", "unreachable")):
        return "no hay conexión a internet"
    return "no se pudo completar la búsqueda web"


async def search_products(
    name: str,
    max_price: float,
    *,
    search_fn: SearchFn | None = None,
    only_available: bool = False,
    limit: int = MAX_PRODUCTS,
    inspect_pages: bool = True,
    page_client: httpx.AsyncClient | None = None,
) -> list[Product]:
    """Busca ``name`` en todo internet (DuckDuckGo) y filtra por precio.

    Lanza las consultas de ``build_queries`` una tras otra hasta reunir
    ``MIN_PRODUCTS`` ofertas válidas o agotarlas. De cada resultado se extraen
    título, precio (€ o USD), dominio y stock. Los resultados sin precio
    reconocible se descartan; si el texto no dice que esté agotado, el producto
    se conserva como disponible o con stock sin confirmar (``None``).

    ``max_price`` se compara con el importe tal cual, sin convertir divisas.

    Args:
        name: Producto a buscar.
        max_price: Precio máximo, inclusive.
        search_fn: Función de búsqueda ``query -> resultados``; por defecto
            DuckDuckGo. Útil en tests.
        only_available: Si es ``True``, descarta lo que se sabe agotado.
        limit: Número máximo de ofertas devueltas.
        inspect_pages: Si es ``True``, se descarga la página de los resultados
            sin precio en el snippet (y, después, de los que superan
            ``max_price`` en menos de ``RECHECK_PRICE_MARGIN``) y se busca en
            el HTML el precio actual, rebajas incluidas
            (``enrich_with_page_prices``).
        page_client: Cliente HTTP para esas páginas; útil en tests.

    Returns:
        Hasta ``limit`` ofertas con ``price <= max_price`` y sin URLs
        repetidas: primero con stock, luego sin confirmar y por último
        agotadas; cada grupo por precio.

    Cada consulta se reintenta con backoff exponencial (``run_search``). Si
    las consultas de ``build_queries`` no dan ningún resultado útil, se
    prueban las de ``relaxed_queries``.

    Raises:
        ValueError: Si ``name`` está vacío o ``max_price`` es negativo.
        SearchError: Si la primera consulta falla tras sus reintentos (red,
            DNS, rate-limit…). Si falla una posterior se devuelve lo reunido.
    """
    name = name.strip()
    if not name:
        raise ValueError("name no puede estar vacío")
    if max_price < 0:
        raise ValueError("max_price no puede ser negativo")

    offers: dict[str, Product] = {}
    # Candidatos a inspeccionar su página: sin precio en el fragmento y, en
    # segundo lugar, con un precio algo superior al límite (puede estar
    # rebajado en la web aunque el fragmento muestre el precio anterior).
    priceless: dict[str, Mapping[str, str]] = {}
    over_budget: dict[str, Mapping[str, str]] = {}

    def accept(product: Product) -> bool:
        if product.price > max_price or product.url in offers:
            return False
        if not (only_available and product.available is False):
            offers[product.url] = product
        return True

    def ingest(results: list[dict]) -> None:
        for result in results:
            product = parse_web_result(result)
            if product is None:
                if (url := _result_url(result)) and result.get("title"):
                    priceless.setdefault(url, result)
            elif (
                not accept(product)
                and product.price <= max_price * RECHECK_PRICE_MARGIN
            ):
                over_budget.setdefault(product.url, result)

    async def run_queries(queries: list[str], *, strict: bool) -> bool:
        """Lanza las consultas en orden; ``False`` si hubo que abortar."""
        for index, query in enumerate(queries):
            try:
                ingest(await run_search(query, search_fn))
            except Exception as exc:
                logger.warning(
                    "Búsqueda web fallida para %r: %s (%r)",
                    query,
                    describe_search_error(exc),
                    exc,
                )
                if strict and index == 0:
                    raise SearchError(describe_search_error(exc)) from exc
                # Tras un rate-limit, insistir solo empeora el bloqueo.
                return False
            if len(offers) >= MIN_PRODUCTS:
                break
        return True

    queries = build_queries(name)
    healthy = await run_queries(queries, strict=True)
    if healthy and not (offers or priceless or over_budget):
        # 0 resultados útiles: se relaja la consulta (sin términos de compra,
        # símbolos ni detalles) para encontrar al menos algunas ofertas.
        fallback = [q for q in relaxed_queries(name) if q not in queries]
        if fallback:
            logger.info("Sin resultados; probando consultas flexibles: %s", fallback)
            await run_queries(fallback, strict=False)

    pending = [
        result for url, result in (priceless | over_budget).items() if url not in offers
    ]
    if inspect_pages and pending and len(offers) < limit:
        for product in await enrich_with_page_prices(pending, client=page_client):
            accept(product)

    return sorted(offers.values(), key=lambda p: p.sort_key)[:limit]


def _is_no_results(exc: Exception) -> bool:
    return "no results found" in str(exc).casefold()


def _result_url(result: Mapping[str, str]) -> str | None:
    url = result.get("href") or result.get("url") or ""
    return url if url.startswith(("http://", "https://")) else None


# --- Precio desde la propia página ------------------------------------------

PAGE_TIMEOUT = 2.5
MAX_PAGE_FETCHES = 10
MAX_PAGE_BYTES = 1_500_000

# Segmentos de ruta de listados, categorías y búsquedas internas: su precio
# sería el de un artículo cualquiera del listado, no el del resultado.
LISTING_PATH_SEGMENTS = frozenset(
    {
        "c",
        "categoria",
        "categorias",
        "category",
        "categories",
        "catalog",
        "catalogo",
        "listado",
        "listing",
        "buscar",
        "busqueda",
        "search",
        "s",
    }
)
_LISTING_QUERY_KEYS = frozenset({"q", "query", "search", "s", "k", "keyword"})


def is_listing_url(url: str) -> bool:
    """Indica si ``url`` parece un listado o búsqueda interna, no una ficha.

    Compara segmentos completos de la ruta (``/c/`` sí, ``/cable/`` no) y
    detecta búsquedas por parámetros como ``?q=`` o ``?search=``.
    """
    parts = urlsplit(url)
    segments = {s.lower() for s in parts.path.split("/") if s}
    if segments & LISTING_PATH_SEGMENTS:
        return True
    keys = {p.split("=", 1)[0].lower() for p in parts.query.split("&") if p}
    return bool(keys & _LISTING_QUERY_KEYS)


SALE_PRICE_META_KEYS = (
    "product:sale_price:amount",
    "og:sale_price:amount",
    "saleprice",
)
PRICE_META_KEYS = ("product:price:amount", "og:price:amount", "price", "lowprice")
CURRENCY_META_KEYS = (
    "product:sale_price:currency",
    "product:price:currency",
    "og:price:currency",
    "pricecurrency",
)
SALE_END_META_KEY = "product:sale_price_dates:end"
# Tipos de precio de schema.org que no son el precio actual.
_REFERENCE_PRICE_TYPES = ("strikethrough", "listprice", "msrp", "srp", "invoice")
_LISTING_LD_TYPES = frozenset({"itemlist", "searchresultspage", "offercatalog"})

_TAG_WITH_ATTRS = re.compile(r"<(?:meta|span|div|data|link)\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_JSON_LD = re.compile(
    r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
# Además de código, se quita el texto tachado (<del>, <s>, <strike>): suele ser
# el precio anterior a la rebaja.
_NOISE = re.compile(
    r"<(script|style|noscript|svg|del|s|strike)\b.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")


@dataclass(frozen=True, slots=True)
class PagePrice:
    """Precio (y stock, si la página lo indica) extraído del HTML."""

    price: float
    currency: str = "EUR"
    available: bool | None = None


def _attrs(tag: str) -> dict[str, str]:
    return {
        m.group(1).lower(): html_lib.unescape(m.group(2) or m.group(3) or m.group(4))
        for m in _ATTR.finditer(tag)
    }


def _amount(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        price = float(value)
    elif isinstance(value, str) and value.strip():
        price = parse_price(value)
    else:
        return None
    return price if price is not None and price >= MIN_WEB_PRICE else None


def _availability(value: object) -> bool | None:
    """Disponibilidad de schema.org/OpenGraph (``OutOfStock``, ``in stock``…)."""
    text = str(value or "").casefold().replace(" ", "").replace("_", "")
    if any(k in text for k in ("outofstock", "soldout", "discontinued")):
        return False
    if any(k in text for k in ("instock", "limitedavailability", "onlineonly")):
        return True
    return None


def _first_known(*values: bool | None) -> bool | None:
    return next((v for v in values if v is not None), None)


# --- JSON-LD ---


def _ld_types(node: dict) -> set[str]:
    types = node.get("@type") or []
    return {str(t).casefold() for t in (types if isinstance(types, list) else [types])}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _ld_top_nodes(html: str) -> list[dict]:
    """Nodos de primer nivel de todos los bloques JSON-LD (``@graph`` incluido)."""
    nodes: list[dict] = []
    for block in _JSON_LD.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in _as_list(data):
            if isinstance(item, dict):
                nodes.append(item)
                nodes.extend(
                    n for n in _as_list(item.get("@graph")) if isinstance(n, dict)
                )
    return nodes


def _offer_price(offer: dict) -> float | None:
    """Precio actual de una oferta: el menor entre ``salePrice``, ``price``,
    ``lowPrice`` y las ``priceSpecification`` que no sean precio de referencia
    (tachado, PVP recomendado…)."""
    candidates = [offer.get(k) for k in ("salePrice", "price", "lowPrice")]
    for spec in _as_list(offer.get("priceSpecification")):
        if not isinstance(spec, dict):
            continue
        price_type = str(spec.get("priceType") or "").casefold()
        if not any(t in price_type for t in _REFERENCE_PRICE_TYPES):
            candidates.append(spec.get("price"))
    prices = [p for p in map(_amount, candidates) if p is not None]
    return min(prices, default=None)


def _offers(node: dict) -> list[dict]:
    """Ofertas de un ``Product``, desplegando ``AggregateOffer.offers``."""
    found: list[dict] = []
    for offer in _as_list(node.get("offers")):
        if isinstance(offer, dict):
            found.append(offer)
            found.extend(
                o for o in _as_list(offer.get("offers")) if isinstance(o, dict)
            )
    return found


def _price_from_product_node(node: dict) -> PagePrice | None:
    """La oferta más barata con stock del producto (o la más barata si no hay)."""
    candidates: list[PagePrice] = []
    for offer in _offers(node):
        price = _offer_price(offer)
        if price is None:
            continue
        currency = offer.get("priceCurrency") or next(
            (
                s.get("priceCurrency")
                for s in _as_list(offer.get("priceSpecification"))
                if isinstance(s, dict) and s.get("priceCurrency")
            ),
            "EUR",
        )
        available = _first_known(
            _availability(offer.get("availability")),
            _availability(node.get("availability")),
        )
        candidates.append(PagePrice(price, str(currency).upper(), available))
    if not candidates:
        return None
    in_stock = [c for c in candidates if c.available is not False]
    return min(in_stock or candidates, key=lambda c: c.price)


def _is_listing_ld(nodes: list[dict]) -> bool:
    """La página es un listado: varios productos o un ``ItemList`` sin producto."""
    products = sum("product" in _ld_types(n) for n in nodes)
    listing = any(_ld_types(n) & _LISTING_LD_TYPES for n in nodes)
    return products > 1 or (products == 0 and listing)


def _price_from_json_ld(nodes: list[dict]) -> PagePrice | None:
    products = (n for n in nodes if _ld_types(n) & {"product", "productgroup"})
    for node in products:
        if (page := _price_from_product_node(node)) is not None:
            return page
    # Sin Product: la primera oferta suelta con precio (Offer de primer nivel,
    # o anidada en otro tipo como Event o Service).
    stack: list[object] = list(nodes)
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if (price := _offer_price(node)) is not None:
                currency = str(node.get("priceCurrency") or "EUR").upper()
                return PagePrice(
                    price, currency, _availability(node.get("availability"))
                )
            stack.extend(v for v in node.values() if isinstance(v, dict | list))
    return None


# --- Metaetiquetas y microdatos ---


def _sale_expired(end: str | None) -> bool:
    if not end:
        return False
    try:
        return date.fromisoformat(end.strip()[:10]) < date.today()
    except ValueError:
        return False


def _scan_meta(html: str) -> tuple[PagePrice | None, bool | None]:
    """Precio y stock de OpenGraph/microdatos; prefiere la oferta si es menor.

    El stock se devuelve aparte porque puede existir sin precio.
    """
    values: dict[str, str] = {}
    availability: bool | None = None
    for tag in _TAG_WITH_ATTRS.findall(html):
        attrs = _attrs(tag)
        key = (
            attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or ""
        ).lower()
        content = attrs.get("content") or attrs.get("value") or attrs.get("href")
        if not key or content is None:
            continue
        values.setdefault(key, content)
        if key.endswith("availability") and availability is None:
            # "in stock" / "out of stock" (OpenGraph) o URLs schema.org.
            availability = _availability(content)

    regular = next(
        (p for k in PRICE_META_KEYS if (p := _amount(values.get(k))) is not None), None
    )
    sale = None
    if not _sale_expired(values.get(SALE_END_META_KEY)):
        sale = next(
            (
                p
                for k in SALE_PRICE_META_KEYS
                if (p := _amount(values.get(k))) is not None
            ),
            None,
        )
    prices = [p for p in (sale, regular) if p is not None]
    if not prices:
        return None, availability
    currency = next((values[k] for k in CURRENCY_META_KEYS if values.get(k)), "EUR")
    return PagePrice(min(prices), currency.upper(), availability), availability


# --- Texto visible ---


def _visible_text(html: str) -> str:
    text = _TAGS.sub(" ", _NOISE.sub(" ", html))
    return re.sub(r"\s+", " ", html_lib.unescape(text))[:20_000]


def _price_from_text(text: str) -> PagePrice | None:
    found = extract_price_and_currency(text)
    if found is None:
        return None
    return PagePrice(found[0], found[1], detect_stock(text))


def extract_price_from_html(html: str) -> PagePrice | None:
    """Extrae el precio actual (rebajas incluidas) y el stock de una ficha.

    Precio, por orden de fiabilidad:

    1. JSON-LD: la oferta más barata con stock del ``Product``; en cada oferta
       el menor entre ``salePrice``, ``price``, ``lowPrice`` y
       ``priceSpecification`` (ignora el precio tachado / recomendado).
    2. OpenGraph y microdatos: ``product:sale_price:amount`` (si no ha
       caducado) frente a ``product:price:amount``, ``og:price:amount``,
       ``itemprop="price"``, ``name="price"``; gana el menor.
    3. El texto visible, sin precios tachados (``<del>``, "antes 799 €") y
       prefiriendo los marcados como oferta ("ahora 649 €").

    Stock: el ``availability`` de JSON-LD (``OutOfStock``/``Discontinued`` →
    agotado), si no el de las metaetiquetas y, si no, las palabras del texto
    ("agotado", "sin stock", "out of stock"…).

    Returns:
        Precio, moneda y stock, o ``None`` si no hay precio o la página es un
        listado (varios ``Product`` o un ``ItemList`` en JSON-LD).
    """
    nodes = _ld_top_nodes(html)
    if _is_listing_ld(nodes):
        return None
    structured = _price_from_json_ld(nodes)
    meta, meta_available = _scan_meta(html)
    text = _visible_text(html)
    page = structured or meta or _price_from_text(text)
    if page is None:
        return None
    available = _first_known(
        structured.available if structured else None,
        meta_available,
        detect_stock(text),
    )
    return PagePrice(page.price, page.currency, available)


async def fetch_page_price(client: httpx.AsyncClient, url: str) -> PagePrice | None:
    """Descarga ``url`` y extrae su precio. Nunca lanza excepciones.

    Corta a los ``PAGE_TIMEOUT`` segundos y a los ``MAX_PAGE_BYTES`` bytes.
    Devuelve ``None`` si la web no responde, bloquea, no es HTML, no tiene
    precio reconocible o es (o redirige a) un listado según
    :func:`is_listing_url`.
    """
    if is_listing_url(url):
        return None
    try:
        async with asyncio.timeout(PAGE_TIMEOUT):
            async with client.stream("GET", url) as response:
                content_type = response.headers.get("content-type", "").lower()
                if response.status_code != 200 or "html" not in content_type:
                    return None
                if is_listing_url(str(response.url)):
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_PAGE_BYTES:
                        break
                encoding = response.charset_encoding or "utf-8"
        html = b"".join(chunks).decode(encoding, errors="replace")
        return extract_price_from_html(html)
    except Exception as exc:  # red, timeout, bloqueo, codificación…
        logger.debug("Sin precio en %s: %r", url, exc)
        return None


async def enrich_with_page_prices(
    results: list[Mapping[str, str]], *, client: httpx.AsyncClient | None = None
) -> list[Product]:
    """Busca el precio en la página de cada resultado sin precio en el snippet.

    Consulta hasta ``MAX_PAGE_FETCHES`` páginas a la vez con ``asyncio.gather``.
    Los listados/categorías no se descargan, y los resultados cuya página falla
    o no tiene precio se descartan.
    """
    targets = [
        (r, url) for r in results if (url := _result_url(r)) and not is_listing_url(url)
    ]
    targets = targets[:MAX_PAGE_FETCHES]
    if not targets:
        return []

    async def run(http: httpx.AsyncClient) -> list[PagePrice | None]:
        return await asyncio.gather(
            *(fetch_page_price(http, url) for _, url in targets)
        )

    if client is None:
        async with create_client(timeout=PAGE_TIMEOUT) as owned:
            found = await run(owned)
    else:
        found = await run(client)

    return [
        _product_from_result(result, url, page.price, page.currency, page.available)
        for (result, url), page in zip(targets, found, strict=True)
        if page is not None
    ]


def build_queries(name: str) -> list[str]:
    """Consultas a lanzar para ``name``, de la más a la menos específica.

    Si el usuario no escribió ningún término de compra (``PURCHASE_TERMS``), se
    añaden palabras como "comprar" y "precio" para que DuckDuckGo devuelva
    fragmentos con precios y tiendas.
    """
    lowered = name.casefold()
    has_intent = any(
        re.search(rf"(?<!\w){re.escape(term)}(?!\w)", lowered)
        for term in PURCHASE_TERMS
    )
    if has_intent:
        missing = [t for t in ("comprar", "precio") if t not in lowered]
        variants: tuple[str, ...] = ("", " ".join(missing) or "oferta")
    else:
        variants = PURCHASE_QUERY_VARIANTS
    queries = [f"{name} {suffix}".strip() for suffix in variants]
    return list(dict.fromkeys(queries))


RELAXED_MAX_TOKENS = 3


def relaxed_queries(name: str) -> list[str]:
    """Consultas más flexibles para cuando ``build_queries`` no da resultados.

    Quita términos de compra (``PURCHASE_TERMS``), comillas, operadores y
    símbolos, y prueba: el núcleo con "precio", el núcleo solo y, si es
    largo, sus ``RELAXED_MAX_TOKENS`` primeras palabras.
    ``"Comprar 'MSI RTX-4070 Ventus 2X' €"`` → ``["msi rtx 4070 ventus 2x
    precio", "msi rtx 4070 ventus 2x", "msi rtx 4070"]``.
    """
    tokens = [
        t
        for t in re.findall(r"[^\W_]+(?:[.,+][^\W_]+)*", name.casefold())
        if t not in PURCHASE_TERMS
    ]
    if not tokens:
        return []
    core = " ".join(tokens)
    queries = [f"{core} precio", core]
    if len(tokens) > RELAXED_MAX_TOKENS:
        queries.append(" ".join(tokens[:RELAXED_MAX_TOKENS]))
    return list(dict.fromkeys(queries))


# --- Utilidades HTTP (para descargar páginas concretas) ---------------------

REQUEST_DELAY: tuple[float, float] = (1.5, 3.5)
REQUEST_TIMEOUT = 10.0
ACCEPT_LANGUAGE = "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7"
# Variantes reales de Accept-Language de un navegador en español.
ACCEPT_LANGUAGES = (
    ACCEPT_LANGUAGE,
    "es-ES,es;q=0.9",
    "es-ES,es;q=0.9,en;q=0.8",
    "es-ES,es;q=0.8,en-US;q=0.5,en;q=0.3",
)
# Cabeceras que forman la "identidad" de navegador que se rota.
IDENTITY_HEADERS = (
    "User-Agent",
    "Accept-Language",
    "Sec-Ch-Ua",
    "Sec-Ch-Ua-Mobile",
    "Sec-Ch-Ua-Platform",
)
ACCEPT_HTML = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)

_CHROMIUM_RE = re.compile(r"Chrome/(\d+)")
_EDGE_RE = re.compile(r"Edg/(\d+)")
_PLATFORMS = (("Windows", "Windows"), ("Mac OS X", "macOS"), ("Linux", "Linux"))


def describe_network_error(exc: httpx.HTTPError) -> str:
    """Explica en español, sin tecnicismos, por qué falló una petición HTTP.

    Distingue errores de DNS (p. ej. ``[Errno 11001] getaddrinfo failed`` en
    Windows), de conexión, de tiempo de espera y respuestas HTTP de error.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in {403, 429}:
            return f"el servidor ha bloqueado la petición (HTTP {code})"
        return f"el servidor respondió con un error (HTTP {code})"
    if isinstance(exc, httpx.TimeoutException):
        return "el servidor tardó demasiado en responder"
    reason = str(exc).lower()
    if isinstance(exc, httpx.ConnectError) and any(
        marker in reason
        for marker in (
            "getaddrinfo",
            "11001",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
        )
    ):
        return "no se pudo resolver el dominio (fallo de DNS o sin internet)"
    if isinstance(exc, httpx.ConnectError):
        return "no se pudo conectar (sin internet o servidor caído)"
    return f"error de red ({type(exc).__name__})"


@cache
def _user_agent_factory() -> UserAgent:
    return UserAgent(browsers=["Chrome", "Edge"], platforms=["desktop"])


def random_user_agent() -> str:
    """Devuelve un User-Agent aleatorio de Chrome o Edge de escritorio."""
    return _user_agent_factory().random


def browser_headers(user_agent: str) -> dict[str, str]:
    """Cabeceras de navegador coherentes con ``user_agent``.

    Las cabeceras ``Sec-Ch-Ua*`` se derivan de la versión y plataforma del
    User-Agent para que no se contradigan entre sí.
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": ACCEPT_HTML,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    chromium = _CHROMIUM_RE.search(user_agent)
    if chromium is None:
        return headers

    version = chromium.group(1)
    edge = _EDGE_RE.search(user_agent)
    brand = (
        f'"Microsoft Edge";v="{edge.group(1)}"'
        if edge
        else f'"Google Chrome";v="{version}"'
    )
    platform = next(
        (name for token, name in _PLATFORMS if token in user_agent), "Windows"
    )
    headers |= {
        "Sec-Ch-Ua": f'"Chromium";v="{version}", {brand}, "Not.A/Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": f'"{platform}"',
    }
    return headers


def _fetch_site(request: httpx.Request) -> str:
    """``Sec-Fetch-Site`` coherente con el ``Referer`` de la petición."""
    referer = request.headers.get("Referer")
    if not referer:
        return "none"  # navegación directa (como escribir la URL)
    return (
        "same-origin"
        if urlsplit(referer).hostname == request.url.host
        else ("cross-site")
    )


def _identity_rotator() -> Callable[[httpx.Request], Awaitable[None]]:
    """Hook de petición que asigna a cada host su propia identidad aleatoria.

    Cada sitio ve un navegador distinto (User-Agent, ``Sec-Ch-Ua*`` y
    ``Accept-Language`` coherentes entre sí), pero estable dentro del mismo
    sitio, también entre redirecciones: cambiarlo a mitad de visita delata
    a un bot.
    """
    identities: dict[str, dict[str, str]] = {}

    async def rotate(request: httpx.Request) -> None:
        host = request.url.host
        identity = identities.get(host)
        if identity is None:
            headers = browser_headers(random_user_agent())
            headers["Accept-Language"] = random.choice(ACCEPT_LANGUAGES)
            identity = {k: v for k, v in headers.items() if k in IDENTITY_HEADERS}
            identities[host] = identity
        for name in IDENTITY_HEADERS:
            request.headers.pop(name, None)
        request.headers.update(identity)
        request.headers["Sec-Fetch-Site"] = _fetch_site(request)

    return rotate


def create_client(
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> httpx.AsyncClient:
    """Cliente HTTP con cabeceras de navegador realistas.

    Rota la identidad (User-Agent y cabeceras asociadas) por cada sitio
    visitado; ver ``_identity_rotator``.
    """
    base = {k: v for k, v in browser_headers("").items() if k not in IDENTITY_HEADERS}
    return httpx.AsyncClient(
        headers=base,
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
        event_hooks={"request": [_identity_rotator()]},
    )


async def polite_delay(bounds: tuple[float, float] = REQUEST_DELAY) -> None:
    """Espera un tiempo aleatorio entre ``bounds[0]`` y ``bounds[1]`` segundos."""
    await asyncio.sleep(random.uniform(*bounds))


async def fetch_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    params: dict[str, str] | None = None,
    referer: str | None = None,
) -> httpx.Response:
    """Descarga ``url`` tras un retardo aleatorio (``polite_delay``).

    Returns:
        La respuesta HTTP, sin comprobar su estado.
    """
    await polite_delay()
    headers = {"Referer": referer} if referer else None
    return await client.get(url, params=params, headers=headers)
