package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5"
)

func setupRouter() *chi.Mux {
	r := chi.NewRouter()
	r.Post("/hello", handleHello)
	r.Post("/hello-gaeilge", handleHelloGaeilge)
	return r
}

func TestHelloEndpoint(t *testing.T) {
	tests := []struct {
		name           string
		testID         string
		requestBody    string
		expectedStatus int
		expectedBody   map[string]string
	}{
		{
			name:           "Valid name returns 200 and greeting",
			testID:         "EN-001",
			requestBody:    `{"name": "World"}`,
			expectedStatus: http.StatusOK,
			expectedBody:   map[string]string{"message": "Hello, World!"},
		},
		{
			name:           "Empty name returns 400 and error",
			testID:         "EN-002",
			requestBody:    `{"name": ""}`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "name is required"},
		},
		{
			name:           "Missing name field returns 400 and error",
			testID:         "EN-003",
			requestBody:    `{}`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "name is required"},
		},
		{
			name:           "Malformed JSON returns 400 and error",
			testID:         "EN-004",
			requestBody:    `not json`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "invalid request body"},
		},
		{
			name:           "Name exceeding 1000 characters returns 400 and error",
			testID:         "EN-005",
			requestBody:    `{"name": "` + strings.Repeat("a", 1001) + `"}`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "name exceeds maximum length of 1000 characters"},
		},
	}

	router := setupRouter()

	for _, tc := range tests {
		t.Run(tc.testID+"_"+tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/hello", strings.NewReader(tc.requestBody))
			req.Header.Set("Content-Type", "application/json")

			rec := httptest.NewRecorder()
			router.ServeHTTP(rec, req)

			if rec.Code != tc.expectedStatus {
				t.Errorf("expected status %d, got %d", tc.expectedStatus, rec.Code)
			}

			contentType := rec.Header().Get("Content-Type")
			if contentType != "application/json" {
				t.Errorf("expected Content-Type 'application/json', got '%s'", contentType)
			}

			var responseBody map[string]string
			if err := json.NewDecoder(rec.Body).Decode(&responseBody); err != nil {
				t.Fatalf("failed to decode response body: %v", err)
			}

			for key, expectedValue := range tc.expectedBody {
				actualValue, ok := responseBody[key]
				if !ok {
					t.Errorf("expected key '%s' in response body, but not found", key)
					continue
				}
				if actualValue != expectedValue {
					t.Errorf("expected %s='%s', got '%s'", key, expectedValue, actualValue)
				}
			}
		})
	}
}

func TestHelloGaeilgeEndpoint(t *testing.T) {
	tests := []struct {
		name           string
		testID         string
		requestBody    string
		expectedStatus int
		expectedBody   map[string]string
	}{
		{
			name:           "Valid name returns 200 and Irish Gaelic greeting",
			testID:         "GA-001",
			requestBody:    `{"name": "Seán"}`,
			expectedStatus: http.StatusOK,
			expectedBody:   map[string]string{"message": "Dia duit, Seán!"},
		},
		{
			name:           "Empty name returns 400 and error",
			testID:         "GA-002",
			requestBody:    `{"name": ""}`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "name is required"},
		},
		{
			name:           "Missing name field returns 400 and error",
			testID:         "GA-003",
			requestBody:    `{}`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "name is required"},
		},
		{
			name:           "Malformed JSON returns 400 and error",
			testID:         "GA-004",
			requestBody:    `not json`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "invalid request body"},
		},
		{
			name:           "Name exceeding 1000 characters returns 400 and error",
			testID:         "GA-005",
			requestBody:    `{"name": "` + strings.Repeat("a", 1001) + `"}`,
			expectedStatus: http.StatusBadRequest,
			expectedBody:   map[string]string{"error": "name exceeds maximum length of 1000 characters"},
		},
		{
			name:           "Name with Irish fada characters returns 200 with preserved accents",
			testID:         "GA-006",
			requestBody:    `{"name": "Máire Ní Bhriain"}`,
			expectedStatus: http.StatusOK,
			expectedBody:   map[string]string{"message": "Dia duit, Máire Ní Bhriain!"},
		},
	}

	router := setupRouter()

	for _, tc := range tests {
		t.Run(tc.testID+"_"+tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/hello-gaeilge", strings.NewReader(tc.requestBody))
			req.Header.Set("Content-Type", "application/json")

			rec := httptest.NewRecorder()
			router.ServeHTTP(rec, req)

			if rec.Code != tc.expectedStatus {
				t.Errorf("expected status %d, got %d", tc.expectedStatus, rec.Code)
			}

			contentType := rec.Header().Get("Content-Type")
			if contentType != "application/json" {
				t.Errorf("expected Content-Type 'application/json', got '%s'", contentType)
			}

			var responseBody map[string]string
			if err := json.NewDecoder(rec.Body).Decode(&responseBody); err != nil {
				t.Fatalf("failed to decode response body: %v", err)
			}

			for key, expectedValue := range tc.expectedBody {
				actualValue, ok := responseBody[key]
				if !ok {
					t.Errorf("expected key '%s' in response body, but not found", key)
					continue
				}
				if actualValue != expectedValue {
					t.Errorf("expected %s='%s', got '%s'", key, expectedValue, actualValue)
				}
			}
		})
	}
}
