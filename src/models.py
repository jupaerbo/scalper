"""Modelos de datos del scraper."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Product:
    """Oferta de un producto en una tienda.

    Attributes:
        title: Nombre del producto.
        price: Precio en la moneda de la tienda.
        available: ``True`` si hay stock, ``False`` si está agotado y ``None``
            si la fuente no lo indica (habitual en resultados de búsqueda web).
        store_name: Nombre de la tienda de origen.
        url: Enlace absoluto a la ficha del producto.
        currency: Moneda del precio (``EUR`` o ``USD``).
    """

    title: str
    price: float
    available: bool | None
    store_name: str
    url: str
    currency: str = "EUR"

    @property
    def maybe_in_stock(self) -> bool:
        """``True`` salvo que se sepa que está agotado."""
        return self.available is not False

    @property
    def sort_key(self) -> tuple[int, float]:
        """Orden de presentación: con stock, sin confirmar, agotado; y precio."""
        rank = 0 if self.available else (1 if self.available is None else 2)
        return rank, self.price

    @property
    def price_label(self) -> str:
        """Precio con su moneda: ``589.90€`` o ``499.99 USD``."""
        if self.currency == "EUR":
            return f"{self.price:.2f}€"
        return f"{self.price:.2f} {self.currency}"
