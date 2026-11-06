"""Exportación de resultados a JSON o CSV."""

import csv
import json
from dataclasses import asdict, fields
from pathlib import Path

from models import Product

SUPPORTED_FORMATS = (".json", ".csv")
FIELDNAMES = [field.name for field in fields(Product)]


def export_products(products: list[Product], output_path: str | Path) -> Path:
    """Guarda ``products`` en ``output_path`` según su extensión.

    - ``.json``: lista de objetos, indentada y en UTF-8.
    - ``.csv``: cabecera con los campos de ``Product``, en UTF-8 con BOM para
      que Excel muestre bien los acentos.

    Crea los directorios intermedios si no existen y sobrescribe el archivo.

    Args:
        products: Productos a exportar.
        output_path: Ruta del archivo de salida.

    Returns:
        La ruta del archivo escrito.

    Raises:
        ValueError: Si la extensión no es ``.json`` ni ``.csv``.
        OSError: Si no se puede escribir el archivo.
    """
    path = Path(output_path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_FORMATS:
        raise ValueError(
            f"Formato no soportado: '{path.suffix or path.name}'. "
            f"Usa {' o '.join(SUPPORTED_FORMATS)}."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [asdict(product) for product in products]
    if suffix == ".json":
        path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)
    return path
