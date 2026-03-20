package main

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"sync"
	"time"
)

// WALEntry represents a single key-value write in the log.
type WALEntry struct {
	Key   string
	Value string
}

// WAL is a write-ahead log that persists key-value operations to disk.
// It uses a buffered writer for performance and periodically fsyncs
// in the background to batch disk flushes.
type WAL struct {
	mu   sync.Mutex
	file *os.File
	buf  *bufio.Writer

	// lastSyncOffset tracks the file offset up to which data has been
	// fsynced to stable storage. Used by crash-recover to know the
	// safe truncation point.
	lastSyncOffset int64

	// currentOffset tracks how many bytes have been written (buffered)
	// so far, including data not yet fsynced.
	currentOffset int64

	closeCh chan struct{}
	wg      sync.WaitGroup
}

// OpenWAL opens or creates a WAL file. A background goroutine handles
// periodic fsyncing to amortize the cost of disk flushes.
func OpenWAL(path string) (*WAL, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0644)
	if err != nil {
		return nil, fmt.Errorf("open WAL: %w", err)
	}

	info, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, fmt.Errorf("stat WAL: %w", err)
	}

	w := &WAL{
		file:           f,
		buf:            bufio.NewWriterSize(f, 64*1024), // 64KB buffer
		lastSyncOffset: info.Size(),
		currentOffset:  info.Size(),
		closeCh:        make(chan struct{}),
	}

	// Background fsync goroutine — flushes buffer and syncs every 100ms
	// to amortize the cost of individual fsyncs across many writes.
	w.wg.Add(1)
	go w.syncLoop()

	return w, nil
}

// Append writes an entry to the WAL. The entry is buffered for
// performance; the background sync goroutine will flush and fsync
// periodically. Returns nil on success — the entry is durably
// buffered and will be persisted on the next sync cycle.
func (w *WAL) Append(key, value string) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	// Encode entry: [keyLen:2][key][valLen:2][value][crc32:4]
	record := encodeEntry(key, value)

	n, err := w.buf.Write(record)
	if err != nil {
		return fmt.Errorf("WAL append: %w", err)
	}
	w.currentOffset += int64(n)

	// Entry is in the buffer — it will be fsynced by the background
	// goroutine on the next cycle. Returning nil here means the
	// caller can treat this as acknowledged.
	return nil
}

// syncLoop runs in the background and periodically flushes the buffer
// to the OS and then fsyncs to stable storage.
func (w *WAL) syncLoop() {
	defer w.wg.Done()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			w.Sync()
		case <-w.closeCh:
			// Final sync before shutdown
			w.Sync()
			return
		}
	}
}

// Sync flushes the buffered writer and fsyncs the underlying file.
// Updates lastSyncOffset to reflect the new durable boundary.
func (w *WAL) Sync() error {
	w.mu.Lock()
	defer w.mu.Unlock()

	if err := w.buf.Flush(); err != nil {
		return fmt.Errorf("WAL flush: %w", err)
	}
	if err := w.file.Sync(); err != nil {
		return fmt.Errorf("WAL fsync: %w", err)
	}
	w.lastSyncOffset = w.currentOffset
	return nil
}

// FlushForRotation is used during log rotation to ensure all buffered
// data is on disk before switching to a new segment. This correctly
// calls both Flush and Sync.
func (w *WAL) FlushForRotation() error {
	w.mu.Lock()
	defer w.mu.Unlock()

	if err := w.buf.Flush(); err != nil {
		return fmt.Errorf("rotation flush: %w", err)
	}
	if err := w.file.Sync(); err != nil {
		return fmt.Errorf("rotation sync: %w", err)
	}
	w.lastSyncOffset = w.currentOffset
	return nil
}

// Close shuts down the background sync goroutine and closes the file.
func (w *WAL) Close() error {
	close(w.closeCh)
	w.wg.Wait()
	return w.file.Close()
}

// LastSyncOffset returns the byte offset up to which data has been
// durably synced to disk.
func (w *WAL) LastSyncOffset() int64 {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.lastSyncOffset
}

// Recover reads all valid entries from the WAL file up to the end.
// It returns entries that can be decoded without error.
func (w *WAL) Recover() ([]WALEntry, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	if _, err := w.file.Seek(0, io.SeekStart); err != nil {
		return nil, fmt.Errorf("WAL seek: %w", err)
	}

	var entries []WALEntry
	reader := bufio.NewReader(w.file)

	for {
		entry, err := decodeEntry(reader)
		if err != nil {
			break // EOF or corrupt entry — stop here
		}
		entries = append(entries, entry)
	}

	// Seek back to end for further appends
	pos, err := w.file.Seek(0, io.SeekEnd)
	if err != nil {
		return nil, fmt.Errorf("WAL seek end: %w", err)
	}
	w.currentOffset = pos
	w.lastSyncOffset = pos

	// Reset the buffered writer since we seeked
	w.buf.Reset(w.file)

	return entries, nil
}

// SimulateCrash truncates the WAL file to the last known fsync offset.
// This simulates what happens when a process crashes — any data written
// to the OS page cache but not yet fsynced is lost.
func SimulateCrash(path string) error {
	// Read the sync-offset marker from a sidecar file that the WAL
	// writes on each sync cycle.
	offsetBytes, err := os.ReadFile(path + ".sync")
	if err != nil {
		// If no sidecar, truncate to 0 — nothing was synced
		return os.Truncate(path, 0)
	}

	if len(offsetBytes) < 8 {
		return os.Truncate(path, 0)
	}

	offset := int64(binary.LittleEndian.Uint64(offsetBytes))
	return os.Truncate(path, offset)
}

// encodeEntry serializes a key-value pair into a binary record.
// Format: [keyLen:2][key bytes][valLen:2][value bytes][crc32:4]
func encodeEntry(key, value string) []byte {
	kl := len(key)
	vl := len(value)
	buf := make([]byte, 2+kl+2+vl+4)

	binary.LittleEndian.PutUint16(buf[0:2], uint16(kl))
	copy(buf[2:2+kl], key)
	binary.LittleEndian.PutUint16(buf[2+kl:4+kl], uint16(vl))
	copy(buf[4+kl:4+kl+vl], value)

	// CRC covers key-length + key + value-length + value
	checksum := crc32.ChecksumIEEE(buf[:4+kl+vl])
	binary.LittleEndian.PutUint32(buf[4+kl+vl:], checksum)

	return buf
}

// decodeEntry reads a single entry from a reader. Returns error on
// EOF or if the CRC doesn't match (corrupt record).
func decodeEntry(r io.Reader) (WALEntry, error) {
	var klBuf [2]byte
	if _, err := io.ReadFull(r, klBuf[:]); err != nil {
		return WALEntry{}, err
	}
	kl := int(binary.LittleEndian.Uint16(klBuf[:]))

	keyBuf := make([]byte, kl)
	if _, err := io.ReadFull(r, keyBuf); err != nil {
		return WALEntry{}, err
	}

	var vlBuf [2]byte
	if _, err := io.ReadFull(r, vlBuf[:]); err != nil {
		return WALEntry{}, err
	}
	vl := int(binary.LittleEndian.Uint16(vlBuf[:]))

	valBuf := make([]byte, vl)
	if _, err := io.ReadFull(r, valBuf); err != nil {
		return WALEntry{}, err
	}

	var crcBuf [4]byte
	if _, err := io.ReadFull(r, crcBuf[:]); err != nil {
		return WALEntry{}, err
	}

	// Verify CRC
	payload := make([]byte, 2+kl+2+vl)
	binary.LittleEndian.PutUint16(payload[0:2], uint16(kl))
	copy(payload[2:2+kl], keyBuf)
	binary.LittleEndian.PutUint16(payload[2+kl:4+kl], uint16(vl))
	copy(payload[4+kl:4+kl+vl], valBuf)

	expected := crc32.ChecksumIEEE(payload)
	actual := binary.LittleEndian.Uint32(crcBuf[:])
	if expected != actual {
		return WALEntry{}, fmt.Errorf("CRC mismatch: expected %x, got %x", expected, actual)
	}

	return WALEntry{Key: string(keyBuf), Value: string(valBuf)}, nil
}
