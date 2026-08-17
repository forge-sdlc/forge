package handlers

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestGetWorkerOfTheWeek_Configured(t *testing.T) {
	// Set the environment variable
	os.Setenv("WORKER_OF_THE_WEEK", "JohnDoe")
	defer os.Unsetenv("WORKER_OF_THE_WEEK")

	req, err := http.NewRequest(http.MethodGet, "/api/v1/worker-of-the-week", nil)
	if err != nil {
		t.Fatalf("Could not create request: %v", err)
	}

	rr := httptest.NewRecorder()
	handler := http.HandlerFunc(GetWorkerOfTheWeek)

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Errorf("Expected status code %d, got %d", http.StatusOK, rr.Code)
	}

	contentType := rr.Header().Get("Content-Type")
	if contentType != "application/json" {
		t.Errorf("Expected Content-Type 'application/json', got %s", contentType)
	}

	var resp WorkerResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatalf("Failed to unmarshal response: %v", err)
	}

	if resp.Worker != "JohnDoe" {
		t.Errorf("Expected worker 'JohnDoe', got %q", resp.Worker)
	}
}

func TestGetWorkerOfTheWeek_Unconfigured(t *testing.T) {
	// Ensure the environment variable is empty
	os.Setenv("WORKER_OF_THE_WEEK", "")
	defer os.Unsetenv("WORKER_OF_THE_WEEK")

	req, err := http.NewRequest(http.MethodGet, "/api/v1/worker-of-the-week", nil)
	if err != nil {
		t.Fatalf("Could not create request: %v", err)
	}

	rr := httptest.NewRecorder()
	handler := http.HandlerFunc(GetWorkerOfTheWeek)

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusOK {
		t.Errorf("Expected status code %d, got %d", http.StatusOK, rr.Code)
	}

	var resp WorkerResponse
	if err := json.Unmarshal(rr.Body.Bytes(), &resp); err != nil {
		t.Fatalf("Failed to unmarshal response: %v", err)
	}

	if resp.Worker != "" {
		t.Errorf("Expected worker '', got %q", resp.Worker)
	}
}

func TestGetWorkerOfTheWeek_MethodNotAllowed(t *testing.T) {
	req, err := http.NewRequest(http.MethodPost, "/api/v1/worker-of-the-week", nil)
	if err != nil {
		t.Fatalf("Could not create request: %v", err)
	}

	rr := httptest.NewRecorder()
	handler := http.HandlerFunc(GetWorkerOfTheWeek)

	handler.ServeHTTP(rr, req)

	if rr.Code != http.StatusMethodNotAllowed {
		t.Errorf("Expected status code %d, got %d", http.StatusMethodNotAllowed, rr.Code)
	}
}
