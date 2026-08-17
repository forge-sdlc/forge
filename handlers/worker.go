package handlers

import (
	"encoding/json"
	"net/http"
	"os"
)

// WorkerResponse represents the JSON response structure for the worker of the week.
type WorkerResponse struct {
	Worker string `json:"worker"`
}

// GetWorkerOfTheWeek handles the GET request for the worker of the week.
func GetWorkerOfTheWeek(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusMethodNotAllowed)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": "Method Not Allowed"})
		return
	}

	worker := os.Getenv("WORKER_OF_THE_WEEK")

	resp := WorkerResponse{
		Worker: worker,
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(resp)
}
