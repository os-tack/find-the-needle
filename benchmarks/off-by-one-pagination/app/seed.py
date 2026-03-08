"""Seed data generator for development and testing."""

import random

ADJECTIVES = [
    "Premium", "Organic", "Vintage", "Classic", "Modern",
    "Compact", "Deluxe", "Essential", "Professional", "Ultra",
]

NOUNS = [
    "Widget", "Gadget", "Tool", "Device", "Sensor",
    "Module", "Adapter", "Connector", "Controller", "Switch",
]


def generate_products(count: int = 50) -> list[dict]:
    """Generate a list of mock products."""
    products = []
    for i in range(1, count + 1):
        adj = ADJECTIVES[(i - 1) % len(ADJECTIVES)]
        noun = NOUNS[(i - 1) % len(NOUNS)]
        products.append({
            "id": i,
            "name": f"{adj} {noun}",
            "price": round(random.uniform(5.0, 99.99), 2),
        })
    return products


if __name__ == "__main__":
    for p in generate_products(10):
        print(p)
