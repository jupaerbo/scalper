# Instructions for Claude Code (Token & Speed Optimization)

## Context & Token Saving Rules
- Avoid printing entire source files when modifying specific functions; make targeted edits.
- Limit command output verbosity. Do not pipe full logs unless requested.
- Use concise tool operations and inspect only required modules to preserve context window.
- Reply in Spanish, extremely concise: no intros, summaries or sign-offs; no theory unless asked.
- Do not repeat code or content already in the conversation.

## Speed & Tooling Standards
- Prefer using `uv` (if installed) over standard `pip` for package execution and virtual environments.
- For linting and formatting, always use: `uv run ruff check --fix .` (or `ruff check --fix .`).
- For running test suites, use fast execution: `uv run pytest -q` (or `pytest -q`).
  - `pyproject.toml` already sets `addopts = "-q"`, so `-q` hides the `N passed` summary line; use `uv run pytest` to see the count, `-x` to stop at the first failure.
- Keep test suite passing (390+ tests in green) before concluding any task.
- Without `uv run`, use `.venv\Scripts\python.exe -m ...`: the global Python lacks the project dependencies (`httpx`, `ddgs`, …).

## Architecture Notes
- Scraper logic lives in `src/scraper.py` (uses Serper API primary, ddgs fallback, strict domain filter `is_valid_store_url`).
- Telegram bot logic lives in `src/telegram_bot.py` (per-chat state in `chat_data`: pending searches keep their inline buttons usable until pressed; in memory only, lost on restart).
- Modules import each other by plain name (`from models import Product`); `src/__init__.py` adds `src/` to `sys.path` so `python -m src.<module>` works.

## Code & Safety Rules
- PEP 8 via ruff; type hints on public functions. Tests in `tests/test_*.py`, never hitting the network (mock with `httpx.MockTransport` / `search_fn`).
- Do not read or modify data files (`*.csv`, `*.json`, `*.sqlite3`), logs or `.venv` unless asked.
- Never run the real CLI search (`python -m src.main`) for testing: `.env` has `TELEGRAM_CHAT_ID`, so it sends real Telegram messages. Call scraper functions directly instead.
- `.env` holds real secrets (bot token, API keys): never print its values.
