import csv
import json
from pathlib import Path

import pytest

from exporter import export_products
from models import Product

PRODUCTS = [
    Product(
        'RTX 4070 Ñandú, edición "OC"', 589.9, True, "Tienda A", "https://a.test/1"
    ),
    Product("RTX 4070 Super", 599.0, False, "Tienda B", "https://b.test/2"),
]


def test_exports_json(tmp_path: Path) -> None:
    path = export_products(PRODUCTS, tmp_path / "resultados.json")

    assert path == tmp_path / "resultados.json"
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {
            "title": 'RTX 4070 Ñandú, edición "OC"',
            "price": 589.9,
            "available": True,
            "store_name": "Tienda A",
            "url": "https://a.test/1",
            "currency": "EUR",
        },
        {
            "title": "RTX 4070 Super",
            "price": 599.0,
            "available": False,
            "store_name": "Tienda B",
            "url": "https://b.test/2",
            "currency": "EUR",
        },
    ]
    assert "Ñandú" in path.read_text(encoding="utf-8")


def test_exports_csv(tmp_path: Path) -> None:
    path = export_products(PRODUCTS, str(tmp_path / "resultados.CSV"))

    with path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    assert rows == [
        {
            "title": 'RTX 4070 Ñandú, edición "OC"',
            "price": "589.9",
            "available": "True",
            "store_name": "Tienda A",
            "url": "https://a.test/1",
            "currency": "EUR",
        },
        {
            "title": "RTX 4070 Super",
            "price": "599.0",
            "available": "False",
            "store_name": "Tienda B",
            "url": "https://b.test/2",
            "currency": "EUR",
        },
    ]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("vacio.json", "[]\n"),
        ("vacio.csv", "title,price,available,store_name,url,currency\n"),
    ],
)
def test_exports_empty_list(tmp_path: Path, filename: str, expected: str) -> None:
    path = export_products([], tmp_path / filename)

    assert path.read_text(encoding="utf-8-sig").replace("\r\n", "\n") == expected


def test_creates_missing_directories(tmp_path: Path) -> None:
    path = export_products(PRODUCTS, tmp_path / "a" / "b" / "out.json")

    assert path.is_file()


def test_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    target.write_text("contenido viejo", encoding="utf-8")

    export_products(PRODUCTS[:1], target)

    assert len(json.loads(target.read_text(encoding="utf-8"))) == 1


@pytest.mark.parametrize("filename", ["out.txt", "out.xlsx", "sin_extension"])
def test_rejects_unsupported_extension(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError, match="Formato no soportado"):
        export_products(PRODUCTS, tmp_path / filename)

    assert not (tmp_path / filename).exists()
