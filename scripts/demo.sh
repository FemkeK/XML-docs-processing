#!/usr/bin/env bash
# Walks through the full flow against a running stack.
#
#   docker compose up -d --build
#   ./scripts/demo.sh
#
# Requires curl and jq.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
API_KEY="${API_KEY:-dev-key}"
SAMPLES="$(cd "$(dirname "$0")/../samples" && pwd)"

step() { printf '\n\033[1;34m== %s ==\033[0m\n' "$1"; }

# POST a sample file. Prints the response body, then the HTTP status on the last line.
submit() {
  curl -sS -X POST "$BASE_URL/v1/documents" \
    -H "X-API-Key: $API_KEY" -H "Content-Type: application/xml" \
    --data-binary "@$SAMPLES/$1" -w '\n%{http_code}'
}

api_get() { curl -sS -H "X-API-Key: $API_KEY" "$BASE_URL$1"; }

# Submit a document and print the status code and response.
show_submit() {
  local response status body
  response=$(submit "$1")
  status=${response##*$'\n'}
  body=${response%$'\n'*}
  echo "HTTP $status"
  echo "$body" | jq .
}

# Submit a document that should be accepted; prints only its id.
submit_accepted() {
  local response
  response=$(submit "$1")
  echo "${response%$'\n'*}" | jq -r .id
}

# Poll until the document reaches COMPLETED or FAILED.
wait_for_final_status() {
  local status
  for _ in $(seq 1 60); do
    status=$(api_get "/v1/documents/$1" | jq -r .status)
    echo "  status: $status" >&2
    if [[ "$status" == "COMPLETED" || "$status" == "FAILED" ]]; then
      return
    fi
    sleep 0.5
  done
  echo "Timeout waiting for document $1" >&2
  exit 1
}

step "Health check (no API key needed)"
curl -sS "$BASE_URL/health" | jq .

step "Request without API key -> 401"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' "$BASE_URL/v1/documents"

step "Malformed XML -> 400"
show_submit malformed.xml

step "XML that violates the schema -> 422 with all errors"
show_submit invalid_schema_order.xml

step "XXE attempt -> 400"
show_submit xxe_attack.xml

step "Valid order -> 202, processed by the worker"
id=$(submit_accepted valid_order.xml)
echo "document id: $id"
wait_for_final_status "$id"
echo "Status history:"
api_get "/v1/documents/$id" | jq '.events[] | {status, message}'
echo "Result:"
api_get "/v1/documents/$id/result" | jq .

step "Order with wrong total -> 202, then FAILED during processing"
id=$(submit_accepted total_mismatch_order.xml)
echo "document id: $id"
wait_for_final_status "$id"
api_get "/v1/documents/$id" | jq '{status, attempts, last_error}'

step "Overview of all documents"
api_get "/v1/documents" | jq '.[] | {id, status, attempts}'
