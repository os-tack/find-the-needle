# postgres-migration-schema-drift

## Difficulty
Hard

## Source
Community-submitted

## Environment
Go 1.22, PostgreSQL, Debian Bookworm

## The bug
The Go API server (`app/main.go`) references column `status` in its INSERT and SELECT queries, but migration `002_rename_status.sql` renamed the column to `order_status`. The server starts fine and health checks pass, but any POST to `/orders` or GET to `/orders/:id` returns a 500 error because PostgreSQL rejects the query with "column 'status' does not exist".

## Why Hard
The bug spans multiple layers: SQL migration files, Go application code, and database schema. The agent must understand migration ordering (001 creates `status`, 002 renames it to `order_status`, 003 adds another column), then trace how the Go code's raw SQL queries reference the old column name. The error only surfaces at runtime when the INSERT/SELECT actually hits the database — the code compiles successfully. The agent needs to correlate the migration rename with every SQL query in the application.

## Expected fix
Change `status` to `order_status` in both the INSERT and SELECT SQL queries in `app/main.go`, then rebuild the Go binary.

## Pinned at
Anonymized snapshot, original repo not disclosed
