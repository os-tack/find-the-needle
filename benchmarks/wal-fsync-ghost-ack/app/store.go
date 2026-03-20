package main

import "sync"

// Store is a simple in-memory key-value store that is rebuilt
// from the WAL on recovery.
type Store struct {
	mu   sync.RWMutex
	data map[string]string
}

// NewStore creates an empty key-value store.
func NewStore() *Store {
	return &Store{
		data: make(map[string]string),
	}
}

// Put sets a key to a value.
func (s *Store) Put(key, value string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.data[key] = value
}

// Get retrieves the value for a key. Returns ("", false) if not found.
func (s *Store) Get(key string) (string, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	v, ok := s.data[key]
	return v, ok
}
