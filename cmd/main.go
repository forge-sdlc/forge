package main

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/go-chi/chi/v5"
)

// HelloRequest represents the request body for hello endpoints
type HelloRequest struct {
	Name string `json:"name"`
}

// HelloResponse represents the response body for hello endpoints
type HelloResponse struct {
	Message string `json:"message"`
}

// ErrorResponse represents an error response
type ErrorResponse struct {
	Error string `json:"error"`
}

// handleHello handles POST /hello requests and returns greetings in English
func handleHello(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	var req HelloRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorResponse{Error: "invalid request body"})
		return
	}

	name := strings.TrimSpace(req.Name)
	if name == "" {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorResponse{Error: "name is required"})
		return
	}

	if utf8.RuneCountInString(name) > 1000 {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorResponse{Error: "name exceeds maximum length of 1000 characters"})
		return
	}

	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(HelloResponse{Message: "Hello, " + name + "!"})
}

// handleHelloGaeilge handles POST /hello-gaeilge requests and returns greetings in Irish Gaelic
func handleHelloGaeilge(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")

	var req HelloRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorResponse{Error: "invalid request body"})
		return
	}

	name := strings.TrimSpace(req.Name)
	if name == "" {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorResponse{Error: "name is required"})
		return
	}

	if utf8.RuneCountInString(name) > 1000 {
		w.WriteHeader(http.StatusBadRequest)
		json.NewEncoder(w).Encode(ErrorResponse{Error: "name exceeds maximum length of 1000 characters"})
		return
	}

	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(HelloResponse{Message: "Dia duit, " + name + "!"})
}

func main() {
	r := chi.NewRouter()

	r.Post("/hello", handleHello)
	r.Post("/hello-gaeilge", handleHelloGaeilge)

	log.Println("Starting server on :3000")
	if err := http.ListenAndServe(":3000", r); err != nil {
		log.Fatal(err)
	}
}
