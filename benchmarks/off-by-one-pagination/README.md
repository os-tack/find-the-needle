# off-by-one-pagination

## Project

A Flask API that serves a paginated product catalog.

## Symptoms

When fetching consecutive pages of products, items are skipped at page boundaries. Page 1 returns the correct items, but page 2 and beyond are offset by one. Clients report missing products when scrolling through the catalog.

## Bug description

The pagination logic produces incorrect offsets for pages after the first. The test verifies that items transition cleanly across page boundaries with no gaps or overlaps.

## Difficulty

Easy

## Expected turns

3-5
