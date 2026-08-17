package main

import (
	"log"
	"net/http"

	"forge/handlers"
)

func main() {
	http.HandleFunc("/api/v1/worker-of-the-week", handlers.GetWorkerOfTheWeek)

	log.Println("Starting API gateway on :8080...")
	if err := http.ListenAndServe(":8080", nil); err != nil {
		log.Fatalf("Server failed to start: %v", err)
	}
}
