package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

const metadataRoot = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default"

type metadataTokens struct {
	http   *http.Client
	mu     sync.Mutex
	token  string
	expiry time.Time
}

func (m *metadataTokens) request(ctx context.Context, suffix string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, metadataRoot+suffix, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Metadata-Flavor", "Google")
	res, err := m.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK || res.Header.Get("Metadata-Flavor") != "Google" {
		return nil, fmt.Errorf("metadata returned HTTP %d", res.StatusCode)
	}
	return io.ReadAll(io.LimitReader(res.Body, 32<<10))
}

func (m *metadataTokens) access(ctx context.Context) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.token != "" && time.Until(m.expiry) > time.Minute {
		return m.token, nil
	}
	body, err := m.request(ctx, "/token")
	if err != nil {
		return "", err
	}
	var result struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &result); err != nil || result.AccessToken == "" || result.ExpiresIn <= 0 {
		return "", errors.New("invalid metadata access token response")
	}
	m.token = result.AccessToken
	m.expiry = time.Now().Add(time.Duration(result.ExpiresIn) * time.Second)
	return m.token, nil
}

func (m *metadataTokens) identity(audience string) tokenSource {
	return func(ctx context.Context) (string, error) {
		body, err := m.request(ctx, "/identity?"+url.Values{"audience": {audience}}.Encode())
		if err != nil {
			return "", err
		}
		identity := strings.TrimSpace(string(body))
		if strings.Count(identity, ".") != 2 {
			return "", errors.New("invalid metadata ID token response")
		}
		return identity, nil
	}
}

type googleClient struct {
	http  *http.Client
	token tokenSource
}

type googleError struct {
	status int
	code   string
}

func (e googleError) Error() string {
	return fmt.Sprintf("Google API returned HTTP %d (%s)", e.status, e.code)
}

func (c googleClient) call(ctx context.Context, method, address string, body, target any) error {
	var requestBody io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			return err
		}
		requestBody = bytes.NewReader(data)
	}
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, method, address, requestBody)
	if err != nil {
		return err
	}
	token, err := c.token(ctx)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.http.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	decoder := json.NewDecoder(io.LimitReader(response.Body, 5<<20))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var result struct {
			Error struct {
				Status string `json:"status"`
			} `json:"error"`
		}
		_ = decoder.Decode(&result)
		return googleError{status: response.StatusCode, code: result.Error.Status}
	}
	if target != nil {
		return decoder.Decode(target)
	}
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 2<<20))
	return nil
}

type firestoreStore struct {
	api  googleClient
	base string
}

type firestoreField struct {
	String string `json:"stringValue"`
}

type firestoreRecord struct {
	Name       string                    `json:"name,omitempty"`
	UpdateTime string                    `json:"updateTime,omitempty"`
	Fields     map[string]firestoreField `json:"fields,omitempty"`
}

func (s firestoreStore) read(ctx context.Context, collection, id string, dst any) (string, error) {
	if !recordName.MatchString(collection) || !recordName.MatchString(id) {
		return "", errNotFound
	}
	var record firestoreRecord
	err := s.api.call(ctx, http.MethodGet, s.base+"/"+collection+"/"+id, nil, &record)
	var serviceError googleError
	if errors.As(err, &serviceError) && serviceError.status == 404 {
		return "", errNotFound
	}
	if err != nil {
		return "", err
	}
	if record.UpdateTime == "" {
		return "", errors.New("Firestore document has no update time")
	}
	return record.UpdateTime, json.Unmarshal([]byte(record.Fields["state"].String), dst)
}

func (s firestoreStore) write(ctx context.Context, collection, id, expected string, value any) error {
	if !recordName.MatchString(collection) || !recordName.MatchString(id) {
		return errNotFound
	}
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if len(data) > 950*1024 {
		return inputError("保存できるサイズを超えました。入力データを小さくしてください")
	}
	query := url.Values{}
	if expected == "" {
		query.Set("currentDocument.exists", "false")
	} else {
		query.Set("currentDocument.updateTime", expected)
	}
	address := s.base + "/" + collection + "/" + id + "?" + query.Encode()
	record := firestoreRecord{Fields: map[string]firestoreField{"state": {String: string(data)}}}
	err = s.api.call(ctx, http.MethodPatch, address, record, nil)
	var serviceError googleError
	if errors.As(err, &serviceError) &&
		(serviceError.status == 409 || serviceError.code == "FAILED_PRECONDITION" ||
			serviceError.status == 404 && expected != "") {
		return errConflict
	}
	return err
}

func (s firestoreStore) listIDs(ctx context.Context, collection string) ([]string, error) {
	if !recordName.MatchString(collection) {
		return nil, errNotFound
	}
	query := url.Values{"pageSize": {"100"}, "mask.fieldPaths": {"marker"}}
	var ids []string
	for {
		var result struct {
			Documents []firestoreRecord `json:"documents"`
			NextToken string            `json:"nextPageToken"`
		}
		if err := s.api.call(ctx, http.MethodGet, s.base+"/"+collection+"?"+query.Encode(), nil, &result); err != nil {
			return nil, err
		}
		for _, record := range result.Documents {
			if _, id, ok := strings.Cut(strings.TrimPrefix(record.Name, s.documentsPrefix()), collection+"/"); ok &&
				recordName.MatchString(id) {
				ids = append(ids, id)
			}
		}
		if result.NextToken == "" {
			return ids, nil
		}
		if result.NextToken == query.Get("pageToken") {
			return nil, errors.New("Firestore pagination repeated a page token")
		}
		query.Set("pageToken", result.NextToken)
	}
}

func (s firestoreStore) documentsPrefix() string {
	_, prefix, _ := strings.Cut(s.base, "/v1/")
	return prefix + "/"
}

type cloudTasksQueue struct {
	api      googleClient
	name     string
	callback string
	secret   string
}

func (q cloudTasksQueue) enqueue(ctx context.Context, id string) error {
	requestBody, err := json.Marshal(map[string]string{"job_id": id})
	if err != nil {
		return err
	}
	body := map[string]any{
		"task": map[string]any{
			"name":             q.name + "/tasks/shift-" + id,
			"dispatchDeadline": "85s",
			"httpRequest": map[string]any{
				"httpMethod": "POST", "url": q.callback, "body": requestBody,
				"headers": map[string]string{
					"Content-Type":       "application/json",
					"X-Shift-Task-Token": q.secret,
				},
			},
		},
	}
	err = q.api.call(ctx, http.MethodPost, "https://cloudtasks.googleapis.com/v2/"+q.name+"/tasks", body, nil)
	var serviceError googleError
	if errors.As(err, &serviceError) && serviceError.code == "ALREADY_EXISTS" {
		return nil
	}
	return err
}

func cloudBackends(project, optimizerURL, adminToken string, httpClient *http.Client) (store, jobQueue, tokenSource, string, error) {
	location, queue := os.Getenv("GCP_TASKS_LOCATION"), os.Getenv("GCP_TASKS_QUEUE")
	database := env("FIRESTORE_DATABASE", "(default)")
	publicURL := strings.TrimRight(os.Getenv("PUBLIC_URL"), "/")
	taskToken := os.Getenv("TASK_CALLBACK_TOKEN")
	validOrigin := func(raw string) bool {
		u, err := url.Parse(raw)
		return err == nil && u.Scheme == "https" && u.Host != "" && u.User == nil &&
			u.Path == "" && u.RawQuery == "" && u.Fragment == ""
	}
	if !recordName.MatchString(project) || !recordName.MatchString(location) || !recordName.MatchString(queue) ||
		(database != "(default)" && !recordName.MatchString(database)) {
		return nil, nil, nil, "", errors.New("configure GCP_PROJECT, GCP_TASKS_LOCATION, GCP_TASKS_QUEUE and FIRESTORE_DATABASE")
	}
	if !validOrigin(publicURL) || !validOrigin(optimizerURL) || len(adminToken) < 16 || len(taskToken) < 16 || taskToken == adminToken {
		return nil, nil, nil, "", errors.New("Cloud Run requires HTTPS origins and distinct administrator and callback tokens (16+ characters)")
	}
	metadata := &metadataTokens{http: httpClient}
	client := googleClient{http: httpClient, token: metadata.access}
	firestore := firestoreStore{api: client,
		base: "https://firestore.googleapis.com/v1/projects/" + project + "/databases/" + database + "/documents"}
	tasks := cloudTasksQueue{api: client, name: "projects/" + project + "/locations/" + location + "/queues/" + queue,
		callback: publicURL + "/internal/jobs/run", secret: taskToken}
	return firestore, tasks, metadata.identity(optimizerURL), taskToken, nil
}
