#!/bin/bash
set -euo pipefail

show_help() {
    echo "Usage: $0 <TICKET-ID>"
    echo ""
    echo "Send a Jira webhook payload to a local Forge instance."
    echo "Substitutes the ticket ID into the payload and, for revision/question"
    echo "payloads, fetches the latest comment from Jira automatically."
    echo ""
    echo "Arguments:"
    echo "  TICKET-ID    Jira ticket key (e.g., AISOS-123)"
    echo ""
    echo "Options:"
    echo "  -h, --help   Show this help message"
    echo ""
    echo "Requires: .env file with JIRA_BASE_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN"
    echo "Payloads: tests/payloads/*.json"
}

if [ $# -eq 0 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    show_help
    exit 0
fi

if [ $# -ne 1 ]; then
    show_help
    exit 1
fi

TICKET="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PAYLOADS_DIR="$PROJECT_ROOT/tests/payloads"
ENV_FILE="$PROJECT_ROOT/.env"

# Load Jira credentials from .env
JIRA_BASE_URL=""
JIRA_USER_EMAIL=""
JIRA_API_TOKEN=""
if [ -f "$ENV_FILE" ]; then
    JIRA_BASE_URL=$(grep '^JIRA_BASE_URL=' "$ENV_FILE" | cut -d= -f2- || true)
    JIRA_USER_EMAIL=$(grep '^JIRA_USER_EMAIL=' "$ENV_FILE" | cut -d= -f2- || true)
    JIRA_API_TOKEN=$(grep '^JIRA_API_TOKEN=' "$ENV_FILE" | cut -d= -f2- || true)
fi

mapfile -t FILES < <(ls "$PAYLOADS_DIR" | grep '^[0-9]')

if [ ${#FILES[@]} -eq 0 ]; then
    echo "No payload files found in $PAYLOADS_DIR"
    exit 1
fi

echo "Select a payload for ticket $TICKET:"
echo ""
for i in "${!FILES[@]}"; do
    printf "  %2d) %s\n" $((i + 1)) "${FILES[$i]}"
done
echo ""
read -rp "Enter number: " choice

if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt ${#FILES[@]} ]; then
    echo "Invalid selection"
    exit 1
fi

FILE="${FILES[$((choice - 1))]}"

# Fetch issue metadata (type, labels, summary) and latest comment from Jira
COMMENT_FILE=$(mktemp /tmp/forge-wh-comment.XXXXXX)
ISSUE_FILE=$(mktemp /tmp/forge-wh-issue.XXXXXX)
PAYLOAD_FILE=$(mktemp /tmp/forge-wh-payload.XXXXXX)
trap 'rm -f "$COMMENT_FILE" "$ISSUE_FILE"' EXIT
HAS_COMMENT=false
HAS_ISSUE=false

if [ -n "$JIRA_BASE_URL" ] && [ -n "$JIRA_USER_EMAIL" ] && [ -n "$JIRA_API_TOKEN" ]; then
    # Fetch issue type, labels, and summary
    echo "Fetching issue metadata for $TICKET ..."
    if curl -sf -u "$JIRA_USER_EMAIL:$JIRA_API_TOKEN" \
        "$JIRA_BASE_URL/rest/api/3/issue/$TICKET?fields=issuetype,labels,summary,status" \
        -H "Accept: application/json" > "$ISSUE_FILE" 2>/dev/null; then
        HAS_ISSUE=true
        ISSUE_TYPE=$(python3 -c "import sys,json; print(json.load(open('$ISSUE_FILE'))['fields']['issuetype']['name'])" 2>/dev/null || true)
        echo "Issue type: $ISSUE_TYPE"
    else
        echo "Warning: Could not fetch issue from Jira, using payload defaults"
    fi

    # Fetch latest comment for revision/question payloads
    if echo "$FILE" | grep -qiE "revision|question|forge-ask"; then
        echo "Fetching latest comment from $TICKET ..."
        if curl -sf -u "$JIRA_USER_EMAIL:$JIRA_API_TOKEN" \
            "$JIRA_BASE_URL/rest/api/3/issue/$TICKET/comment" \
            -H "Accept: application/json" | \
            python3 -c "
import sys, json

data = json.load(sys.stdin)
comments = data.get('comments', [])
if not comments:
    sys.exit(1)

last = comments[-1]
body = last.get('body', '')

if isinstance(body, dict):
    def extract_text(node):
        if isinstance(node, str):
            return node
        text = node.get('text', '')
        for child in node.get('content', []):
            text += extract_text(child)
        return text
    body = extract_text(body)

body = body.strip()
if not body:
    sys.exit(1)

print(body)
" > "$COMMENT_FILE" 2>/dev/null; then
            HAS_COMMENT=true
            echo "Latest comment: $(head -c 100 "$COMMENT_FILE")..."
        else
            echo "Warning: Could not fetch comment from Jira, using payload default"
        fi
    fi
    echo ""
else
    echo "Warning: Jira credentials not found in .env, using payload defaults"
    echo ""
fi

echo "Sending $FILE with ticket $TICKET ..."
echo ""

# Build the final payload: substitute ticket ID, issue metadata, and comment
sed "s/TEST-123/$TICKET/g" "$PAYLOADS_DIR/$FILE" | \
    python3 -c "
import sys, json

payload = json.load(sys.stdin)

# Inject real issue metadata from Jira
issue_file = '$ISSUE_FILE'
has_issue = '$HAS_ISSUE' == 'true'
if has_issue:
    with open(issue_file) as f:
        issue_data = json.load(f)
    fields = issue_data.get('fields', {})
    payload['issue']['fields']['issuetype'] = fields.get('issuetype', payload['issue']['fields']['issuetype'])
    payload['issue']['fields']['status'] = fields.get('status', payload['issue']['fields']['status'])
    payload['issue']['fields']['summary'] = fields.get('summary', payload['issue']['fields']['summary'])
    payload['issue']['fields']['labels'] = fields.get('labels', payload['issue']['fields'].get('labels', []))

# Inject latest comment from Jira
comment_file = '$COMMENT_FILE'
has_comment = '$HAS_COMMENT' == 'true'
if has_comment and 'comment' in payload:
    with open(comment_file) as f:
        payload['comment']['body'] = f.read().strip()

json.dump(payload, sys.stdout, indent=2)
" > "$PAYLOAD_FILE"

echo "Payload saved to: $PAYLOAD_FILE"
echo ""

curl -s -X POST http://localhost:8000/api/v1/webhooks/jira \
    -H "Content-Type: application/json" \
    -d @"$PAYLOAD_FILE"

echo ""
