package main

import (
	"bufio"
	"fmt"
	"net"
	"os"
	"strings"
)

// WriteBatch connects to the server and sends N SET commands.
// Returns the list of keys that received +OK acknowledgments.
func WriteBatch(addr string, n int) []string {
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "client: connect failed: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()

	scanner := bufio.NewScanner(conn)
	var acked []string

	for i := 0; i < n; i++ {
		key := fmt.Sprintf("key-%04d", i)
		value := fmt.Sprintf("val-%04d", i)
		cmd := fmt.Sprintf("SET %s=%s\n", key, value)

		_, err := conn.Write([]byte(cmd))
		if err != nil {
			fmt.Fprintf(os.Stderr, "client: write error: %v\n", err)
			os.Exit(1)
		}

		if !scanner.Scan() {
			fmt.Fprintf(os.Stderr, "client: read error: %v\n", scanner.Err())
			os.Exit(1)
		}
		resp := scanner.Text()
		if resp == "+OK" {
			acked = append(acked, key)
		} else {
			fmt.Fprintf(os.Stderr, "client: SET %s got %s\n", key, resp)
		}
	}

	return acked
}

// VerifyBatch connects to the server and sends GET for each key in
// the comma-separated list. Prints LOST or OK for each, and a summary.
func VerifyBatch(addr string, keyList string) {
	keys := strings.Split(keyList, ",")
	if len(keys) == 0 {
		fmt.Println("no keys to verify")
		return
	}

	conn, err := net.Dial("tcp", addr)
	if err != nil {
		fmt.Fprintf(os.Stderr, "client: connect failed: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()

	scanner := bufio.NewScanner(conn)
	lost := 0
	ok := 0

	for _, key := range keys {
		cmd := fmt.Sprintf("GET %s\n", key)
		_, err := conn.Write([]byte(cmd))
		if err != nil {
			fmt.Fprintf(os.Stderr, "client: write error: %v\n", err)
			os.Exit(1)
		}

		if !scanner.Scan() {
			fmt.Fprintf(os.Stderr, "client: read error: %v\n", scanner.Err())
			os.Exit(1)
		}
		resp := scanner.Text()
		if resp == "-NOTFOUND" {
			fmt.Printf("LOST %s\n", key)
			lost++
		} else if strings.HasPrefix(resp, "+") {
			ok++
		} else {
			fmt.Printf("ERROR %s: %s\n", key, resp)
			lost++
		}
	}

	fmt.Printf("summary: %d OK, %d LOST out of %d acked\n", ok, lost, len(keys))
}
