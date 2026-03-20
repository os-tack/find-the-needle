package main

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"net"
	"os"
	"strings"
)

// Server handles TCP connections for the key-value store.
// Protocol is line-based:
//   SET key=value  → +OK\n  or  -ERR ...\n
//   GET key        → +value\n  or  -NOTFOUND\n
type Server struct {
	addr  string
	store *Store
	wal   *WAL
}

// NewServer creates a server bound to the given address.
func NewServer(addr string, store *Store, wal *WAL) *Server {
	return &Server{
		addr:  addr,
		store: store,
		wal:   wal,
	}
}

// ListenAndServe starts accepting TCP connections.
func (s *Server) ListenAndServe() error {
	ln, err := net.Listen("tcp", s.addr)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	defer ln.Close()

	// Write a ready marker so clients know we're up
	fmt.Fprintf(os.Stderr, "kvwal: listening on %s\n", s.addr)

	for {
		conn, err := ln.Accept()
		if err != nil {
			return fmt.Errorf("accept: %w", err)
		}
		go s.handleConn(conn)
	}
}

func (s *Server) handleConn(conn net.Conn) {
	defer conn.Close()
	scanner := bufio.NewScanner(conn)

	for scanner.Scan() {
		line := scanner.Text()
		resp := s.handleCommand(line)
		fmt.Fprintf(conn, "%s\n", resp)
	}
}

func (s *Server) handleCommand(line string) string {
	parts := strings.SplitN(line, " ", 2)
	if len(parts) == 0 {
		return "-ERR empty command"
	}

	cmd := strings.ToUpper(parts[0])
	switch cmd {
	case "SET":
		if len(parts) < 2 {
			return "-ERR usage: SET key=value"
		}
		kv := strings.SplitN(parts[1], "=", 2)
		if len(kv) != 2 {
			return "-ERR usage: SET key=value"
		}
		key, value := kv[0], kv[1]

		// Write to WAL first — this is the "durable" path.
		// BUG: Append only buffers the data; fsync happens in the
		// background. But we send +OK immediately, leading the client
		// to believe the write is durable.
		if err := s.wal.Append(key, value); err != nil {
			return fmt.Sprintf("-ERR wal: %v", err)
		}

		// Update the sync-offset sidecar so crash-recover knows
		// the safe truncation point.
		s.writeSyncOffset()

		// Apply to in-memory store
		s.store.Put(key, value)

		return "+OK"

	case "GET":
		if len(parts) < 2 {
			return "-ERR usage: GET key"
		}
		key := parts[1]
		val, ok := s.store.Get(key)
		if !ok {
			return "-NOTFOUND"
		}
		return "+" + val

	default:
		return "-ERR unknown command: " + cmd
	}
}

// writeSyncOffset writes the WAL's last-synced offset to a sidecar
// file so crash-recover can truncate correctly.
func (s *Server) writeSyncOffset() {
	offset := s.wal.LastSyncOffset()
	buf := make([]byte, 8)
	binary.LittleEndian.PutUint64(buf, uint64(offset))
	// Best-effort write — if this fails the crash-recover will
	// truncate to 0 which is safe (just loses more data).
	os.WriteFile(walPath+".sync", buf, 0644)
}
