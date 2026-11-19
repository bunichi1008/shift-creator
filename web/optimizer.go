package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

type optimizer interface {
	solve(context.Context, SolveRequest) (SolveResult, error)
	validate(context.Context, ValidationRequest) (Report, error)
}

type tokenSource func(context.Context) (string, error)

type optimizerClient struct {
	url   string
	http  *http.Client
	token tokenSource
}

func (c optimizerClient) solve(ctx context.Context, request SolveRequest) (SolveResult, error) {
	var result SolveResult
	err := c.request(ctx, "/v1/solve", request, &result)
	return result, err
}

func (c optimizerClient) validate(ctx context.Context, request ValidationRequest) (Report, error) {
	var report Report
	err := c.request(ctx, "/v1/validate", request, &report)
	return report, err
}

func (c optimizerClient) request(ctx context.Context, path string, value, dst any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url+path, bytes.NewReader(data))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	if c.token != nil {
		token, err := c.token(ctx)
		if err != nil {
			return err
		}
		request.Header.Set("Authorization", "Bearer "+token)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()

	body := json.NewDecoder(io.LimitReader(response.Body, 4<<20))
	if response.StatusCode == http.StatusUnprocessableEntity {
		var problem struct {
			Detail []struct {
				Message string `json:"msg"`
			} `json:"detail"`
		}
		if body.Decode(&problem) == nil && len(problem.Detail) > 0 {
			return inputError("入力を確認してください（" + strings.TrimPrefix(problem.Detail[0].Message, "Value error, ") + "）")
		}
		return inputError("最適化サービスに渡す入力を確認してください")
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("optimizer returned HTTP %d", response.StatusCode)
	}
	return body.Decode(dst)
}
