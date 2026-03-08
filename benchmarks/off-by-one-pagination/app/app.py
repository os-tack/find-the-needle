"""Product catalog API with paginated listing."""

from flask import Flask, jsonify, request

app = Flask(__name__)

# Simulated product database
PRODUCTS = [
    {"id": i, "name": f"Product {i}", "price": round(9.99 + i * 1.50, 2)}
    for i in range(1, 51)
]

PER_PAGE = 10


@app.route("/products")
def list_products():
    """Return a paginated list of products."""
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", PER_PAGE, type=int)

    if page < 1:
        return jsonify({"error": "Page must be >= 1"}), 400
    if per_page < 1 or per_page > 100:
        return jsonify({"error": "per_page must be between 1 and 100"}), 400

    # Calculate offset for pagination
    offset = (page - 1) * per_page - (1 if page > 1 else 0)
    items = PRODUCTS[offset : offset + per_page]

    return jsonify({
        "page": page,
        "per_page": per_page,
        "total": len(PRODUCTS),
        "total_pages": (len(PRODUCTS) + per_page - 1) // per_page,
        "items": items,
    })


@app.route("/products/<int:product_id>")
def get_product(product_id):
    """Return a single product by ID."""
    for product in PRODUCTS:
        if product["id"] == product_id:
            return jsonify(product)
    return jsonify({"error": "Product not found"}), 404


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
