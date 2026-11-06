# CLAUDE.md

## Estilo de respuesta (ahorro de tokens)
- Sé extremadamente conciso. Sin introducciones, resúmenes ni despedidas.
- Muestra solo diffs o los bloques de código modificados, nunca archivos completos sin cambios.
- Omite explicaciones teóricas; explica solo si se pide explícitamente.
- No repitas código ni contenido que ya esté en la conversación.
- Lee solo las partes de los archivos que necesites.

## Stack
- Python 3.15, entorno virtual en `.venv`; gestor de paquetes `uv`.
- Configuración de ruff y pytest en `pyproject.toml`.
- Activa `.venv` antes de `python -m ...` (o usa `.venv\Scripts\python.exe -m ...`); el Python global no tiene `httpx` ni `bs4`.
- Tests: `python -m pytest -q` (`-x` para cortar en el primer fallo).
- Lint/format: `python -m ruff check --fix .` y `python -m ruff format .`.

## Reglas de código
- Sigue PEP 8 (lo aplica ruff). Usa type hints en funciones públicas.
- Antes de dar una tarea por terminada, ejecuta `python -m ruff check .` y `python -m pytest -q`.
- Tests en `tests/`, archivos `test_*.py`.
- No leas ni modifiques datos (`*.csv`, `*.json`, `*.sqlite3`), logs ni `.venv` salvo que se pida.
