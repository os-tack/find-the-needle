# race-condition-counter

A Node.js Express application that maintains a shared counter backed by Redis. The service exposes endpoints to increment, read, and reset the counter. Under concurrent load, the counter loses updates -- firing 100 simultaneous increments consistently results in a final value significantly less than 100. The service works perfectly under sequential access.

Difficulty: MEDIUM
