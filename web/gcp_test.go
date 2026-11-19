package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestFirestoreVersionPreconditionsAndListing(t *testing.T) {
	const docPrefix = "projects/test-project/databases/(default)/documents/"
	const stamp = "2026-11-19T04:12:01.123456Z"
	var writes int
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		w := httptest.NewRecorder()
		if r.Header.Get("Authorization") != "Bearer google-access" {
			t.Error("missing GCP authorization")
		}
		switch {
		case r.Method == http.MethodPatch:
			writes++
			query := r.URL.Query()
			switch writes {
			case 1:
				if query.Get("currentDocument.exists") != "false" {
					t.Error("missing create-only precondition")
				}
			case 2:
				if query.Get("currentDocument.updateTime") != stamp {
					t.Error("missing compare-and-swap precondition")
				}
				jsonResponse(w, 400, map[string]any{"error": map[string]string{"status": "FAILED_PRECONDITION"}})
				return w.Result(), nil
			}
			var record firestoreRecord
			if err := json.NewDecoder(r.Body).Decode(&record); err != nil || record.Fields["state"].String != "\"hello\"" {
				t.Error("incorrect Firestore record")
			}
			jsonResponse(w, 200, record)
		case strings.HasSuffix(r.URL.Path, "/shift-jobs"):
			result := map[string]any{"documents": []firestoreRecord{{Name: docPrefix + "shift-jobs/first"}}}
			if r.URL.Query().Get("pageToken") == "" {
				result["nextPageToken"] = "two"
			} else {
				result["documents"] = []firestoreRecord{{Name: docPrefix + "shift-jobs/second"}}
			}
			jsonResponse(w, 200, result)
		default:
			jsonResponse(w, 200, firestoreRecord{
				UpdateTime: stamp, Fields: map[string]firestoreField{"state": {String: "\"hello\""}},
			})
		}
		return w.Result(), nil
	})
	persistence := firestoreStore{
		api: googleClient{http: &http.Client{Transport: transport}, token: func(context.Context) (string, error) {
			return "google-access", nil
		}},
		base: "https://firestore.test/v1/" + strings.TrimSuffix(docPrefix, "/"),
	}
	ctx := context.Background()
	if err := persistence.write(ctx, plans, currentPlanID, "", "hello"); err != nil {
		t.Fatal(err)
	}
	var state string
	version, err := persistence.read(ctx, plans, currentPlanID, &state)
	if err != nil || version != stamp || state != "hello" {
		t.Fatalf("incorrect document: %q, %q, %v", state, version, err)
	}
	if err := persistence.write(ctx, plans, currentPlanID, version, "changed"); !errors.Is(err, errConflict) {
		t.Fatalf("expected stale write conflict, got %v", err)
	}
	ids, err := persistence.listIDs(ctx, jobs)
	if err != nil || strings.Join(ids, ",") != "first,second" {
		t.Fatalf("incorrect pagination: %v, %v", ids, err)
	}
}

func TestCloudTasksUsesStableNameAndAuthenticatedCallback(t *testing.T) {
	name := "projects/test-project/locations/asia-northeast1/queues/shifts"
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		w := httptest.NewRecorder()
		var submitted struct {
			Task struct {
				Name string `json:"name"`
				HTTP struct {
					Body    string            `json:"body"`
					URL     string            `json:"url"`
					Headers map[string]string `json:"headers"`
				} `json:"httpRequest"`
			} `json:"task"`
		}
		d := json.NewDecoder(r.Body)
		if err := d.Decode(&submitted); err != nil {
			t.Error(err)
		}
		payload, err := base64.StdEncoding.DecodeString(submitted.Task.HTTP.Body)
		if err != nil || string(payload) != `{"job_id":"task-id"}` ||
			submitted.Task.Name != name+"/tasks/shift-task-id" ||
			submitted.Task.HTTP.URL != "https://web.test/internal/jobs/run" ||
			submitted.Task.HTTP.Headers["X-Shift-Task-Token"] != "test-callback" {
			t.Error("incorrect Cloud Tasks callback")
		}
		jsonResponse(w, 409, map[string]any{"error": map[string]string{"status": "ALREADY_EXISTS"}})
		return w.Result(), nil
	})
	queue := cloudTasksQueue{
		api: googleClient{http: &http.Client{Transport: transport}, token: func(context.Context) (string, error) {
			return "google-access", nil
		}},
		name: name, callback: "https://web.test/internal/jobs/run", secret: "test-callback",
	}
	if err := queue.enqueue(context.Background(), "task-id"); err != nil {
		t.Fatal(err)
	}
}

func TestMetadataAccessCacheAndAudience(t *testing.T) {
	const audience = "https://optimizer.test"
	var accessCalls int
	transport := roundTripFunc(func(r *http.Request) (*http.Response, error) {
		if r.Header.Get("Metadata-Flavor") != "Google" {
			t.Error("missing metadata request header")
		}
		w := httptest.NewRecorder()
		w.Header().Set("Metadata-Flavor", "Google")
		if strings.HasSuffix(r.URL.Path, "/identity") {
			if r.URL.Query().Get("audience") != audience {
				t.Error("missing optimizer audience")
			}
			_, _ = io.WriteString(w, "header.payload.signature")
		} else {
			accessCalls++
			jsonResponse(w, 200, map[string]any{"access_token": "access", "expires_in": 3600})
		}
		return w.Result(), nil
	})
	m := &metadataTokens{http: &http.Client{Transport: transport}}
	for range 2 {
		if token, err := m.access(context.Background()); err != nil || token != "access" {
			t.Fatalf("invalid access token response %q: %v", token, err)
		}
	}
	if accessCalls != 1 {
		t.Fatalf("expected metadata access token caching, got %d calls", accessCalls)
	}
	if token, err := m.identity(audience)(context.Background()); err != nil || token != "header.payload.signature" {
		t.Fatalf("invalid ID token %q: %v", token, err)
	}
}
