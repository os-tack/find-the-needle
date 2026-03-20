# wal-fsync-ghost-ack

## Project

A Go key-value store backed by a write-ahead log (WAL). The server accepts SET/GET commands over TCP and persists writes to a WAL file before applying them to an in-memory store. On recovery after a crash, the WAL is replayed to rebuild state. The WAL uses buffered I/O with periodic background fsyncing for performance.

## Symptoms

After writing 50 entries and receiving acknowledgments for all of them, the server is killed (simulating a crash). The WAL is truncated to its last fsync point and the server is restarted. Upon verifying, some acknowledged keys are missing — they were lost in the crash. The number of lost keys varies between runs but is typically 10-40 out of 50.

## Bug description

The WAL's Append() method writes the entry to a bufio.Writer and returns success immediately. The server sends +OK to the client as soon as Append() returns. However, the actual fsync to disk happens in a background goroutine on a 100ms ticker. If the server crashes between an ack and the next fsync cycle, all writes since the last sync are lost — even though the client received +OK for each one. The lastSyncOffset field tracks the durable boundary correctly but is never consulted before sending acknowledgments. A separate FlushForRotation() method in the WAL correctly calls both Flush() and Sync(), which serves as misdirection.

## Difficulty

Hard

## Expected turns

10-20
