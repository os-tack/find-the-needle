# retry-storm-duplicate-transfer

## Project

A Python banking service with three components: a gateway (port 8080) that accepts transfer requests and retries on failure, a processor (port 8081) that executes fund transfers with idempotency tracking, and a chaos proxy (port 8082) that sits between them and can delay responses to simulate network issues. All services use a shared SQLite database.

## Symptoms

When a transfer of 500 from account A to B is initiated and the chaos proxy delays responses beyond the gateway's timeout, the gateway retries the request. After all retries complete, account A has been debited multiple times (e.g., A=0, B=2000 instead of A=500, B=1500). The total money in the system is no longer conserved. The idempotency key mechanism that should prevent this doesn't work correctly under concurrent retries.

## Bug description

In the processor's execute_transfer() function, the debit (subtracting from the source account) happens BEFORE the idempotency check. When the gateway retries due to a timeout, the original request is still being processed by the chaos proxy (response delayed). The retry hits the processor while the first request is paused between the debit and the idempotency insert. Both requests pass the idempotency check (neither has recorded the key yet), and both debit the account. The result is a double-debit. The idempotency key is correctly checked, but the check happens at the wrong point in the transaction — after the irreversible side effect.

## Difficulty

Hard

## Expected turns

10-20
