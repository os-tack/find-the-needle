"""Data models for the product catalog."""


class Product:
    """Represents a product in the catalog."""

    def __init__(self, id: int, name: str, price: float):
        self.id = id
        self.name = name
        self.price = price

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "price": self.price}

    def __repr__(self) -> str:
        return f"Product(id={self.id}, name={self.name!r}, price={self.price})"


class Category:
    """Represents a product category."""

    def __init__(self, id: int, name: str):
        self.id = id
        self.name = name
        self.products: list[Product] = []

    def add_product(self, product: Product):
        self.products.append(product)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "product_count": len(self.products),
        }
