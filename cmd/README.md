# HelloWorld API Service

A simple Go HTTP API service demonstrating REST endpoints for personalized greetings in multiple languages.

## Prerequisites

- Go 1.21+
- [Chi router](https://github.com/go-chi/chi)

## Running the Server

```bash
cd cmd
go run main.go
```

The server starts on port 3000.

## API Endpoints

### POST /hello

Returns a personalized greeting in English.

**Request:**
```json
{
  "name": "World"
}
```

**Response (200 OK):**
```json
{
  "message": "Hello, World!"
}
```

**Errors:**
- `400 Bad Request` with `{"error": "name is required"}` if name is empty or missing
- `400 Bad Request` with `{"error": "name exceeds maximum length of 1000 characters"}` if name is too long
- `400 Bad Request` with `{"error": "invalid request body"}` if JSON is malformed

### POST /hello-gaeilge

Returns a personalized greeting in Irish Gaelic.

**Request:**
```json
{
  "name": "Seán"
}
```

**Response (200 OK):**
```json
{
  "message": "Dia duit, Seán!"
}
```

**Errors:**
- `400 Bad Request` with `{"error": "name is required"}` if name is empty or missing
- `400 Bad Request` with `{"error": "name exceeds maximum length of 1000 characters"}` if name is too long
- `400 Bad Request` with `{"error": "invalid request body"}` if JSON is malformed

## Running Tests

```bash
cd cmd
go test -v ./...
```

## Example Usage

```bash
# English greeting
curl -X POST http://localhost:3000/hello \
  -H "Content-Type: application/json" \
  -d '{"name": "World"}'
# Response: {"message":"Hello, World!"}

# Irish Gaelic greeting
curl -X POST http://localhost:3000/hello-gaeilge \
  -H "Content-Type: application/json" \
  -d '{"name": "Seán"}'
# Response: {"message":"Dia duit, Seán!"}
```

## Notes

- **Irish Gaelic Greeting**: "Dia duit" (pronounced "DEE-ah gwit") is the traditional Irish greeting, literally meaning "God be with you." This is the most common and culturally appropriate greeting in Irish Gaelic.
- **Fada Support**: The API correctly handles Irish names with fada (accent marks) like "Seán," "Máire," and "Ní" using UTF-8 character counting.
