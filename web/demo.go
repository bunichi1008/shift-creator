package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"regexp"
)

const demoCookie = "__session"

var sessionIDPattern = regexp.MustCompile("^[0-9a-f]{32}$")

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
