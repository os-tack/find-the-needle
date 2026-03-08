"""Utility functions for the product catalog."""


def format_price(price: float) -> str:
    """Format a price as a dollar string."""
    return f"${price:.2f}"


def validate_pagination(page: int, per_page: int) -> tuple[bool, str]:
    """Validate pagination parameters."""
    if page < 1:
        return False, "Page must be >= 1"
    if per_page < 1 or per_page > 100:
        return False, "per_page must be between 1 and 100"
    return True, ""


def calculate_total_pages(total_items: int, per_page: int) -> int:
    """Calculate the total number of pages."""
    return (total_items + per_page - 1) // per_page
