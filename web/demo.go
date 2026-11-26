package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"regexp"
	"time"
)

const (
	demoCookie        = "__session"
	demoProposalUsage = "shift-demo-proposal-usage"
)

var sessionIDPattern = regexp.MustCompile("^[0-9a-f]{32}$")
var errDemoProposalLimit = errors.New("public demo proposal limit reached")

type demoWorkspaceKey struct{}

func demoWorkspace(ctx context.Context) string {
	workspace, _ := ctx.Value(demoWorkspaceKey{}).(string)
	return workspace
}

func (s *server) demoRequest(w http.ResponseWriter, r *http.Request) (*http.Request, bool) {
	cookie, err := r.Cookie(demoCookie)
	if err != nil || !sessionIDPattern.MatchString(cookie.Value) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/plan" {
			jsonResponse(w, http.StatusConflict, map[string]string{"error": "ページを再読み込みしてデモを開いてください"})
			return r, false
		}
		var secret [16]byte
		if _, err := rand.Read(secret[:]); err != nil {
			respondError(w, err)
			return r, false
		}
		cookie = &http.Cookie{
			Name: demoCookie, Value: hex.EncodeToString(secret[:]), Path: "/",
			Secure: s.secureSession || r.TLS != nil, HttpOnly: true, SameSite: http.SameSiteLaxMode,
		}
		http.SetCookie(w, cookie)
	}
	// Persist only a digest. Job URLs and logs can contain it without exposing the cookie.
	digest := sha256.Sum256([]byte(cookie.Value))
	ctx := context.WithValue(r.Context(), demoWorkspaceKey{}, hex.EncodeToString(digest[:16]))
	return r.WithContext(ctx), true
}

func (s *server) reserveDemoProposal(ctx context.Context) error {
	day := s.now().UTC().Format(time.DateOnly)
	for range 5 {
		var calls int
		version, err := s.store.read(ctx, demoProposalUsage, day, &calls)
		if err != nil && !errors.Is(err, errNotFound) {
			return err
		}
		if calls >= s.publicProposalLimit {
			return errDemoProposalLimit
		}
		// Reserve before the external call, which can incur usage even if its response is lost.
		err = s.store.write(ctx, demoProposalUsage, day, version, calls+1)
		if !errors.Is(err, errConflict) {
			return err
		}
	}
	return errBusy
}
