"""Permite ejecutar los módulos como ``python -m src.<módulo>``.

Los módulos se importan entre sí por nombre (``from models import ...``), así
que se añade este directorio a ``sys.path``.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
