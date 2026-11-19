package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
)

var (
	errNotFound = errors.New("record not found")
	errConflict = errors.New("record changed")
	recordName  = regexp.MustCompile("^[a-z0-9_-]{1,80}$")
)

type store interface {
	// An empty expected version means create-only.
	read(context.Context, string, string, any) (string, error)
	write(context.Context, string, string, string, any) error
	listIDs(context.Context, string) ([]string, error)
}

type fileStore struct {
	dir string
	mu  sync.Mutex
}

func (s *fileStore) path(collection, id string) string {
	return filepath.Join(s.dir, collection, id+".json")
}

func (s *fileStore) read(_ context.Context, collection, id string, dst any) (string, error) {
	if !recordName.MatchString(collection) || !recordName.MatchString(id) {
		return "", errNotFound
	}
	data, err := os.ReadFile(s.path(collection, id))
	if errors.Is(err, fs.ErrNotExist) {
		return "", errNotFound
	}
	if err != nil {
		return "", err
	}
	if err := json.Unmarshal(data, dst); err != nil {
		return "", fmt.Errorf("read %s/%s: %w", collection, id, err)
	}
	return fmt.Sprintf("%x", sha256.Sum256(data)), nil
}

func (s *fileStore) write(ctx context.Context, collection, id, expected string, value any) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	var previous json.RawMessage
	version, err := s.read(ctx, collection, id, &previous)
	if err != nil && !errors.Is(err, errNotFound) {
		return err
	}
	if version != expected || !recordName.MatchString(collection) || !recordName.MatchString(id) {
		return errConflict
	}
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	path := s.path(collection, id)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	temp, err := os.CreateTemp(filepath.Dir(path), ".pending-")
	if err != nil {
		return err
	}
	defer os.Remove(temp.Name())
	_, writeErr := temp.Write(data)
	syncErr := temp.Sync()
	closeErr := temp.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	return os.Rename(temp.Name(), path)
}

func (s *fileStore) listIDs(_ context.Context, collection string) ([]string, error) {
	entries, err := os.ReadDir(filepath.Join(s.dir, collection))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var ids []string
	for _, entry := range entries {
		name := entry.Name()
		if id, ok := strings.CutSuffix(name, ".json"); !entry.IsDir() && ok && recordName.MatchString(id) {
			ids = append(ids, id)
		}
	}
	return ids, nil
}
