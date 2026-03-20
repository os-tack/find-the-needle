package main

import (
	"fmt"
	"os"
	"strconv"
)

const (
	walPath    = "/tmp/kvwal.wal"
	listenAddr = "localhost:9876"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintf(os.Stderr, "usage: kvwal <serve|client|crash-recover>\n")
		os.Exit(1)
	}

	switch os.Args[1] {
	case "serve":
		store := NewStore()
		wal, err := OpenWAL(walPath)
		if err != nil {
			fmt.Fprintf(os.Stderr, "failed to open WAL: %v\n", err)
			os.Exit(1)
		}
		// Replay any existing WAL entries into the store
		entries, err := wal.Recover()
		if err != nil {
			fmt.Fprintf(os.Stderr, "WAL recovery failed: %v\n", err)
			os.Exit(1)
		}
		for _, e := range entries {
			store.Put(e.Key, e.Value)
		}
		srv := NewServer(listenAddr, store, wal)
		if err := srv.ListenAndServe(); err != nil {
			fmt.Fprintf(os.Stderr, "server error: %v\n", err)
			os.Exit(1)
		}

	case "client":
		if len(os.Args) < 3 {
			fmt.Fprintf(os.Stderr, "usage: kvwal client <write-batch N | verify-batch KEYS>\n")
			os.Exit(1)
		}
		switch os.Args[2] {
		case "write-batch":
			if len(os.Args) < 4 {
				fmt.Fprintf(os.Stderr, "usage: kvwal client write-batch <count>\n")
				os.Exit(1)
			}
			n, err := strconv.Atoi(os.Args[3])
			if err != nil {
				fmt.Fprintf(os.Stderr, "invalid count: %v\n", err)
				os.Exit(1)
			}
			keys := WriteBatch(listenAddr, n)
			// Output acked keys comma-separated
			for i, k := range keys {
				if i > 0 {
					fmt.Print(",")
				}
				fmt.Print(k)
			}
			fmt.Println()

		case "verify-batch":
			if len(os.Args) < 4 {
				fmt.Fprintf(os.Stderr, "usage: kvwal client verify-batch <key1,key2,...>\n")
				os.Exit(1)
			}
			VerifyBatch(listenAddr, os.Args[3])

		default:
			fmt.Fprintf(os.Stderr, "unknown client command: %s\n", os.Args[2])
			os.Exit(1)
		}

	case "crash-recover":
		// Simulate crash: truncate WAL to last known sync point
		if err := SimulateCrash(walPath); err != nil {
			fmt.Fprintf(os.Stderr, "crash simulation failed: %v\n", err)
			os.Exit(1)
		}
		fmt.Println("crash-recover: WAL truncated to last sync point")

	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n", os.Args[1])
		os.Exit(1)
	}
}
