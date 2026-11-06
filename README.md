# 🛒 Cazaofertas · Bot de Telegram para rastrear precios

[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests: pytest](https://img.shields.io/badge/tests-pytest%20·%20250%2B-0A9EDC?logo=pytest&logoColor=white)](tests/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)
[![Deploy: Render](https://img.shields.io/badge/deploy-Render-46E3B7?logo=render&logoColor=white)](https://render.com/)

Bot de Telegram que **busca un producto en todo internet**, filtra las ofertas por
debajo del precio que indiques y **te avisa cuando cambian el precio o el stock**.
No depende de una lista fija de tiendas: consulta buscadores web, extrae precio,
tienda y disponibilidad de cada resultado y, cuando hace falta, lee la propia ficha
del producto para obtener el precio exacto (incluidas las rebajas).

Incluye también una **CLI** para buscar desde la terminal y exportar a JSON/CSV.

```text
Tú:   RTX 4070
Bot:  Perfecto: RTX 4070. ¿Cuál es tu precio máximo en euros?
Tú:   700
Bot:  🛒 6 oferta(s) ≤ 700.00€ (4 con stock)
      • MSI GeForce RTX 4070 Ventus 2X
        💰 649.00€ · ✅ En stock · tienda-ejemplo.es
      ...
      [🔔 Crear Alerta]  [❌ Cancelar]
```

---

## Índice

- [Características principales](#-características-principales)
- [Cómo funciona](#-cómo-funciona)
- [Instalación y uso local](#-instalación-y-uso-local)
- [Configuración (`.env`)](#-configuración-env)
- [Uso](#-uso)
- [Tests y calidad](#-tests-y-calidad)
- [Despliegue en Render (gratis)](#-despliegue-en-render-gratis)
- [Estructura del proyecto](#-estructura-del-proyecto)
- [Limitaciones conocidas](#-limitaciones-conocidas)

---

## ✨ Características principales

| | Característica | Detalle |
|---|---|---|
| 💬 | **Búsqueda conversacional directa** | En chats privados basta con escribir el producto (ej. `RTX 4070`): el bot solo pregunta el precio máximo. `/buscar` sigue disponible y es obligatorio en grupos. |
| 🌍 | **Scraper global asíncrono** | Busca con [`ddgs`](https://pypi.org/project/ddgs/) sin tiendas fijas. Para los resultados sin precio visible inspecciona la página en paralelo (`asyncio.gather`, 2,5 s de límite por página) leyendo **JSON-LD**, **OpenGraph**/microdatos y, como último recurso, el texto. |
| 🏷️ | **Precios de oferta** | Prioriza `salePrice`, `product:sale_price:amount` o el texto "ahora 649 €"; ignora el precio tachado (`<del>`, "antes", "PVPR", `StrikethroughPrice`). Un producto cuyo snippet supera tu límite se revisa por si la rebaja actual sí entra. |
| 📦 | **Stock real** | `availability` de schema.org (`OutOfStock`, `Discontinued`…), metaetiquetas y frases como "agotado", "sin stock", "avísame cuando…". Los agotados no generan alertas. |
| 🗂️ | **Descarta listados y categorías** | Por la URL (`/categoria/`, `/c/`, `/search/`, `?q=`…) y por el JSON-LD (`ItemList`, varios `Product`), para no atribuir el precio de un listado a un único artículo. |
| 🔔 | **Alertas periódicas** | Repite la búsqueda cada 30 min y solo avisa si hay ofertas nuevas, cambios de precio o de stock. Hasta 5 alertas por chat. |
| 🛡️ | **Resistente a bloqueos** | Reintentos con *backoff* exponencial y *jitter*, rotación de motores de búsqueda, identidad de navegador realista por sitio (User-Agent, `Sec-Ch-Ua`, `Accept-Language`) y consultas de respaldo más flexibles. |
| ❤️ | **Health check para Render** | Servidor HTTP embebido con `aiohttp` que responde `200 OK` en `/`, en el mismo bucle de eventos que el bot. |
| 🧪 | **Suite de pruebas** | 250+ tests con `pytest`, sin red: HTTP simulado con `httpx.MockTransport` y búsquedas inyectadas. |

---

## ⚙️ Cómo funciona

```mermaid
flowchart LR
    U[Usuario en Telegram] -->|"RTX 4070" + 700| B(telegram_bot.py)
    B --> S(scraper.search_products)
    S -->|consultas + reintentos| D[(ddgs: DuckDuckGo, Brave, Mojeek…)]
    D --> P{¿precio en el snippet?}
    P -- sí --> F[Filtro precio ≤ máx.]
    P -- no / supera el límite --> H[Descarga ligera de la ficha<br/>JSON-LD · OpenGraph · texto]
    H --> F
    F --> R[Ordenar: con stock → sin confirmar → agotado]
    R --> B
    B -->|cada 30 min| A[Alertas: solo cambios]
```

---

## 🚀 Instalación y uso local

### Requisitos

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** (gestor de paquetes y entornos): `pip install uv`
- Un bot de Telegram creado con [@BotFather](https://t.me/BotFather)

### 1. Clonar e instalar dependencias

```bash
git clone <url-del-repositorio>
cd <carpeta-del-proyecto>
uv sync            # crea .venv e instala dependencias (incluidas las de desarrollo)
```

### 2. Activar el entorno virtual

| Sistema | Comando |
|---|---|
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` |
| Windows (cmd) | `.venv\Scripts\activate.bat` |
| Linux / macOS | `source .venv/bin/activate` |

> También puedes ejecutar cualquier comando sin activar el entorno anteponiendo
> `uv run`, por ejemplo `uv run python -m pytest`.

### 3. Configurar las variables

```bash
cp .env.example .env    # Windows: copy .env.example .env
```

Rellena al menos `TELEGRAM_BOT_TOKEN` (ver [Configuración](#-configuración-env)).

### 4. Ejecutar los tests

```bash
python -m pytest
```

### 5. Arrancar el bot

```bash
python -m src.telegram_bot
```

El bot empieza a escuchar mensajes (*polling*) y levanta el health check en
`http://localhost:10000/`. Escríbele a tu bot en Telegram un nombre de producto.

---

## 🔐 Configuración (`.env`)

El archivo `.env` se lee automáticamente al arrancar. **Nunca lo subas al
repositorio** (ya está en `.gitignore`).

| Variable | Obligatoria | Descripción |
|---|:---:|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token del bot que te da @BotFather. |
| `TELEGRAM_CHAT_ID` | — | Chat que recibe las alertas de la **CLI** (`python -m src.main`). Si no está, la CLI no envía nada por Telegram. El bot interactivo no la necesita. |
| `PORT` | — | Puerto del health check HTTP. Por defecto `10000`; en Render se define automáticamente. |
| `TELEGRAM_ALLOWED_CHAT_IDS` | — | IDs de chat autorizados, separados por comas. Vacío = cualquiera puede usar el bot. |
| `DDGS_PROXY` | — | Proxy para el buscador (`http://usuario:clave@host:puerto`). Útil si la IP del servidor sufre *rate-limit*. |
| `SEARCH_API_KEY` | — | Clave de una API de búsqueda de pago, usada **solo** si los buscadores gratuitos dan 0 resultados o bloquean (403, 429, captcha). |
| `SEARCH_API_PROVIDER` | — | `serper` (por defecto, [Serper.dev](https://serper.dev)), `serpapi` ([SerpAPI](https://serpapi.com)) o `google` ([Custom Search](https://developers.google.com/custom-search/v1/overview)). |
| `GOOGLE_CSE_ID` | — | ID del buscador programable; obligatorio con `SEARCH_API_PROVIDER=google`. |
| `NOTIFY_EMAIL`, `SMTP_*` | — | Alertas por email desde la CLI (ver `.env.example`). |

Ejemplo mínimo:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:AAH-tu-token-de-botfather
TELEGRAM_CHAT_ID=123456789
PORT=10000
```

> 💡 Para conocer tu `TELEGRAM_CHAT_ID`, escribe a tu bot y abre
> `https://api.telegram.org/bot<TOKEN>/getUpdates`: aparece en `message.chat.id`.
>
> ⚠️ Si tu token se ha mostrado en algún sitio público, revócalo con `/revoke` en
> @BotFather y genera uno nuevo.

---

## 🧭 Uso

### Bot de Telegram

| Comando / acción | Qué hace |
|---|---|
| *Escribir un producto* | Inicia una búsqueda directa (solo chats privados). |
| `/start`, `/buscar` | Inicia una búsqueda guiada (en grupos es obligatorio). |
| `/cancel` | Cancela la búsqueda en curso. |
| `/alertas` | Lista tus alertas activas. |
| `/parar` | Elimina todas tus alertas. |

Tras cada búsqueda, el botón **🔔 Crear Alerta** vigila esa búsqueda cada 30 minutos.

### CLI

```bash
python -m src.main --name "RTX 4070" --max-price 700
python -m src.main -n "RTX 4070" -p 700 -o resultados.csv   # exporta a CSV o JSON
python -m src.main --help
```

| Opción | Descripción |
|---|---|
| `-n`, `--name` | Producto a buscar. |
| `-p`, `--max-price` | Precio máximo (inclusive). |
| `-o`, `--output` | Exporta a `.json` o `.csv`. |
| `--telegram-token`, `--telegram-chat-id` | Envía una alerta por Telegram (por defecto, desde `.env`). |
| `--notify-email` | Envía una alerta por email (requiere `SMTP_*`). |

> ⚠️ Si `.env` tiene `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, **cada búsqueda
> con la CLI envía un mensaje real** a ese chat.

---

## 🧪 Tests y calidad

```bash
python -m pytest              # toda la suite (250+ tests)
python -m pytest -x           # parar en el primer fallo
python -m ruff check --fix .  # lint
python -m ruff format .       # formato
```

Los tests **no hacen peticiones reales**: las páginas se simulan con
`httpx.MockTransport`, el buscador con funciones inyectadas (`search_fn`) y las
esperas del *backoff* se anulan. Cubren extracción de precios (JSON-LD,
OpenGraph, ofertas), stock, listados, reintentos ante *rate-limit*, la
conversación del bot, alertas, exportación y el servidor HTTP.

---

## ☁️ Despliegue en Render (gratis)

El bot funciona por *polling*, pero el plan gratuito de Render solo ofrece
**Web Services**, que deben responder a peticiones HTTP. Por eso el bot levanta
un pequeño servidor `aiohttp` en `PORT` que responde `200 OK` en `/`.

### 1. Crear el servicio

En [dashboard.render.com](https://dashboard.render.com) → **New → Web Service** →
conecta tu repositorio y usa:

| Campo | Valor |
|---|---|
| **Runtime** | Python 3 |
| **Instance Type** | Free |
| **Build Command** | `pip install uv && uv sync --frozen --no-dev` |
| **Start Command** | `.venv/bin/python -m src.telegram_bot` |
| **Health Check Path** | `/` |

### 2. Variables de entorno

En **Environment** añade:

| Variable | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Tu token de @BotFather |
| `PYTHON_VERSION` | Una versión ≥ 3.12, p. ej. `3.13.4` |
| `TELEGRAM_ALLOWED_CHAT_IDS` | *(opcional)* tus IDs de chat |
| `DDGS_PROXY` | *(opcional)* proxy si el buscador limita la IP de Render |
| `SEARCH_API_KEY` | *(opcional, recomendado en Render)* API de respaldo si los buscadores bloquean la IP |

`PORT` lo inyecta Render automáticamente; no hace falta definirlo.

### 3. Mantenerlo despierto con UptimeRobot

Los servicios gratuitos de Render **se suspenden tras ~15 minutos sin tráfico
HTTP entrante**. Como el bot no recibe peticiones (es él quien consulta a
Telegram), se dormiría y dejaría de responder. Para evitarlo:

1. Crea una cuenta gratuita en [UptimeRobot](https://uptimerobot.com/).
2. **Add New Monitor** → tipo **HTTP(s)**.
3. URL: `https://<tu-servicio>.onrender.com/`
4. Intervalo: **5 minutos**.

### Notas importantes

- **Una sola instancia por token.** Si el bot también está corriendo en local,
  Telegram devolverá `Conflict: terminated by other getUpdates request`.
- **Las alertas se guardan en memoria**: se pierden con cada reinicio o
  despliegue y hay que volver a crearlas.
- Las horas gratuitas de Render son limitadas al mes; un servicio despierto
  24/7 las consume casi por completo.

---

## 📁 Estructura del proyecto

```text
.
├── src/
│   ├── telegram_bot.py   # Bot interactivo, alertas y health check (aiohttp)
│   ├── scraper.py        # Búsqueda web, reintentos, extracción de precio/stock
│   ├── main.py           # CLI (typer + rich)
│   ├── notifier.py       # Alertas por Telegram y email
│   ├── exporter.py       # Exportación JSON / CSV
│   ├── models.py         # Modelo Product
│   └── __init__.py       # Permite `python -m src.<módulo>`
├── tests/                # Suite de pytest (sin red)
├── .env.example          # Plantilla de configuración
├── pyproject.toml        # Dependencias y configuración de ruff/pytest
└── uv.lock               # Versiones fijadas
```

---

## ⚠️ Limitaciones conocidas

- Los precios en USD se comparan con tu límite **sin convertir divisa**.
- Algunas webs bloquean descargas automáticas o cargan el precio con
  JavaScript; esos resultados se descartan si el snippet no trae precio.
- Desde IPs de servidores compartidos los buscadores pueden aplicar
  *rate-limit*; el bot reintenta y avisa, pero un proxy (`DDGS_PROXY`) es la
  solución de fondo.
- Respeta los términos de uso de los sitios consultados y no reduzcas los
  intervalos de las alertas.

---

## 📄 Licencia

Distribuido bajo licencia MIT. Consulta el archivo [`LICENSE`](LICENSE).
