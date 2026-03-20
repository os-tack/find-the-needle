package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	_ "github.com/lib/pq"
)

var db *sql.DB

type Order struct {
	ID              int       `json:"id"`
	UserID          int       `json:"user_id"`
	Total           float64   `json:"total"`
	Status          string    `json:"order_status"`
	ShippingAddress string    `json:"shipping_address"`
	CreatedAt       time.Time `json:"created_at"`
}

type CreateOrderRequest struct {
	UserID          int     `json:"user_id"`
	Total           float64 `json:"total"`
	ShippingAddress string  `json:"shipping_address"`
}

func main() {
	var err error
	connStr := "host=/var/run/postgresql dbname=ordersdb sslmode=disable user=postgres"

	for i := 0; i < 30; i++ {
		db, err = sql.Open("postgres", connStr)
		if err == nil {
			err = db.Ping()
			if err == nil {
				break
			}
		}
		log.Printf("Waiting for postgres... (%d/30)", i+1)
		time.Sleep(1 * time.Second)
	}
	if err != nil {
		log.Fatalf("Failed to connect to database: %v", err)
	}
	defer db.Close()

	log.Println("Connected to PostgreSQL")

	http.HandleFunc("/health", healthHandler)
	http.HandleFunc("/orders", ordersHandler)
	http.HandleFunc("/orders/", orderByIDHandler)

	log.Println("Server listening on :8080")
	log.Fatal(http.ListenAndServe(":8080", nil))
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(200)
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

func ordersHandler(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodPost:
		createOrder(w, r)
	default:
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
	}
}

func orderByIDHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	// Extract ID from /orders/{id}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/orders/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		http.Error(w, "Missing order ID", http.StatusBadRequest)
		return
	}
	orderID := parts[0]

	getOrder(w, orderID)
}

func createOrder(w http.ResponseWriter, r *http.Request) {
	var req CreateOrderRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, fmt.Sprintf("Invalid request body: %v", err), http.StatusBadRequest)
		return
	}

	if req.UserID == 0 || req.Total == 0 {
		http.Error(w, "user_id and total are required", http.StatusBadRequest)
		return
	}

	var order Order
	// BUG: Uses old column name "status" — migration 002 renamed it to "order_status".
	// This INSERT will fail with: column "status" does not exist
	err := db.QueryRow(
		`INSERT INTO orders (user_id, total, status, shipping_address)
		 VALUES ($1, $2, 'pending', $3)
		 RETURNING id, user_id, total, status, shipping_address, created_at`,
		req.UserID, req.Total, req.ShippingAddress,
	).Scan(&order.ID, &order.UserID, &order.Total, &order.Status, &order.ShippingAddress, &order.CreatedAt)

	if err != nil {
		log.Printf("ERROR creating order: %v", err)
		http.Error(w, fmt.Sprintf("Failed to create order: %v", err), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(order)
}

func getOrder(w http.ResponseWriter, orderID string) {
	var order Order
	// BUG: Also uses old column name "status" in SELECT
	err := db.QueryRow(
		`SELECT id, user_id, total, status, shipping_address, created_at
		 FROM orders WHERE id = $1`,
		orderID,
	).Scan(&order.ID, &order.UserID, &order.Total, &order.Status, &order.ShippingAddress, &order.CreatedAt)

	if err != nil {
		if err == sql.ErrNoRows {
			http.Error(w, "Order not found", http.StatusNotFound)
			return
		}
		log.Printf("ERROR fetching order: %v", err)
		http.Error(w, fmt.Sprintf("Failed to fetch order: %v", err), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(order)
}
