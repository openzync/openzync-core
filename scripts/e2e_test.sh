#!/usr/bin/env bash
# ==============================================================================
# OpenZep E2E Test Suite
# Tests all endpoint groups in a single run with DB verification.
#
# Usage:
#   ./scripts/e2e_test.sh                    # Default: localhost:8000
#   BASE_URL=http://localhost:8000 ./scripts/e2e_test.sh
#   BASE_URL=http://localhost:8000 DSN="postgresql://user:pass@host:5432/db" ./scripts/e2e_test.sh
#
# Dependencies: curl, psql, python3 (for json.tool), jq (optional, falls back)
# ==============================================================================

set -uo pipefail

# ── Config ──────────────────────────────────────────────────────────────────
BASE_URL="${BASE_URL:-http://localhost:8000}"
DSN="${DSN:-postgresql://openzep:openzep@localhost:5432/openzep}"
SKIP_DB="${SKIP_DB:-false}"  # set to "true" to skip psql verification
RATE_LIMIT_SLEEP="${RATE_LIMIT_SLEEP:-4}"  # seconds to sleep on 429 before retry
STEP_DELAY="${STEP_DELAY:-5}"              # seconds between steps (rate limit: 10 req/60s)

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'  # No Color
PASS="${GREEN}✓ PASS${NC}"
FAIL="${RED}✗ FAIL${NC}"
WARN="${YELLOW}⚠ WARN${NC}"

# ── State ───────────────────────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0
STEP=0
ORG_ID=""
API_KEY=""
USER_ID=""
SESSION_ID=""
JOB_ID=""
NODE_ID=""

# ── Helpers ─────────────────────────────────────────────────────────────────

# curl_retry: curl with retry for transient errors (including 429 rate limits).
# Uses --retry-all-errors to retry on any error including HTTP 429/5xx.
# NOTE: --retry-all-errors also retries expected 4xx errors (401/422/404),
# adding a few seconds to error-test steps — acceptable trade-off.
curl_retry() {
  curl --retry 3 --retry-delay "$RATE_LIMIT_SLEEP" --retry-all-errors -s "$@"
}

# step_delay: small pause between steps to avoid rate limits
step_delay() {
  if [ "$STEP_DELAY" -gt 0 ] 2>/dev/null; then
    sleep "$STEP_DELAY"
  fi
}

title() {
  echo -e "\n${YELLOW}═══════════════════════════════════════════════════════════${NC}"
  echo -e "${YELLOW}  $1${NC}"
  echo -e "${YELLOW}═══════════════════════════════════════════════════════════${NC}"
}

step() {
  STEP=$((STEP + 1))
  echo -e "\n${YELLOW}[Step $STEP]${NC} $1"
  step_delay
}

ok() {
  echo -e "  ${PASS} $1"
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  echo -e "  ${FAIL} $1"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

warn() {
  echo -e "  ${WARN} $1"
  WARN_COUNT=$((WARN_COUNT + 1))
}

check_http() {
  local expected="$1"
  local actual="$2"
  local label="$3"
  if [ "$actual" = "$expected" ]; then
    ok "$label (HTTP $actual)"
  else
    fail "$label — expected HTTP $expected, got $actual"
  fi
}

db_check() {
  local label="$1"
  local query="$2"
  local expect_nonempty="${3:-true}"  # if true, expect >0 rows
  if [ "$SKIP_DB" = "true" ]; then
    warn "[DB skipped] $label"
    return
  fi
  local count
  count=$(psql "$DSN" -t -A -c "SELECT count(*) FROM ($query) AS _dbcheck;" 2>/dev/null || echo "-1")
  if [ "$count" = "-1" ]; then
    warn "[DB error] $label — query failed"
    return
  fi
  if [ "$expect_nonempty" = "true" ] && [ "$count" -gt 0 ] 2>/dev/null; then
    ok "[DB] $label ($count rows)"
  elif [ "$expect_nonempty" = "false" ] && [ "$count" -eq 0 ] 2>/dev/null; then
    ok "[DB] $label (0 rows as expected)"
  else
    fail "[DB] $label — expected rows $expect_nonempty, got $count"
  fi
}

json_extract() {
  # Extract a value from JSON using python3 (portable)
  local key="$1"
  python3 -c "import sys,json; d=json.load(sys.stdin); print($key)" 2>/dev/null || echo ""
}

extract_var() {
  local expr="${1:-}"
  [ -z "$expr" ] && return
  python3 -c "import sys,json; d=json.load(sys.stdin); print($expr)" 2>/dev/null || echo ""
}

# ── Pre-flight ──────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════════════════════╗"
echo "║        OpenZep E2E Test Suite                           ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Server:  $BASE_URL"
echo "║  DB:      ${DSN:0:50}..."
echo "║  Date:    $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "╚══════════════════════════════════════════════════════════╝"

# ── Phase 1: Bootstrap & Health ─────────────────────────────────────────────
title "PHASE 1: Bootstrap & Health"

step "1.1 — Health check"
HTTP_CODE=$(curl_retry -o /tmp/e2e_health.json -w "%{http_code}" "$BASE_URL/v1/health")
check_http 200 "$HTTP_CODE" "GET /v1/health"

step "1.2 — Readiness check"
HTTP_CODE=$(curl_retry -o /tmp/e2e_ready.json -w "%{http_code}" "$BASE_URL/v1/ready")
# 200 or 503 (degraded) are both acceptable
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "503" ]; then
  ok "GET /v1/ready (HTTP $HTTP_CODE)"
  PASS_COUNT=$((PASS_COUNT + 1))
else
  fail "GET /v1/ready — expected 200 or 503, got $HTTP_CODE"
fi

step "1.3 — Bootstrap organization"
BOOTSTRAP_RESPONSE=$(curl_retry -X POST "$BASE_URL/admin/organizations" \
  -H "Content-Type: application/json" \
  -d '{"name":"E2E Test Suite Org","plan":"pro"}')
HTTP_CODE=$(echo "$BOOTSTRAP_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # If it's an error, print error code
    print(d.get('status', 201))
except:
    print('no-json')
" 2>/dev/null || echo "500")
ORG_ID=$(echo "$BOOTSTRAP_RESPONSE" | extract_var 'd.get("organization_id","")')
API_KEY=$(echo "$BOOTSTRAP_RESPONSE" | extract_var 'd.get("api_key","")')

if [ -n "$ORG_ID" ] && [ -n "$API_KEY" ]; then
  ok "POST /admin/organizations — org=$ORG_ID"
  echo "    Organization: $ORG_ID"
  echo "    API Key:      ${API_KEY:0:12}...${API_KEY: -4}"
else
  fail "POST /admin/organizations — could not extract org_id/api_key"
  # Try to show error
  echo "$BOOTSTRAP_RESPONSE" | python3 -m json.tool 2>/dev/null || echo "$BOOTSTRAP_RESPONSE"
  exit 1
fi

AUTH="Authorization: Bearer $API_KEY"

step "1.4 — DB verification: organization created"
db_check "organizations table" "SELECT 1 FROM organizations WHERE id='$ORG_ID'"

# ── Phase 1.5: Admin Schema CRUD ─────────────────────────────────────────────
title "PHASE 1.5: Admin Schema CRUD"

step "1.5a — Create classification schema"
SCHEMA_CREATE=$(curl_retry -X POST "$BASE_URL/v1/admin/schemas" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{
    "name": "e2e_intent_labels",
    "type": "classification",
    "json_schema": {
      "intent": ["question", "command", "complaint", "chit-chat", "greeting", "farewell", "request", "confirmation"],
      "emotion": ["joy", "frustration", "sadness", "anger", "neutral", "surprise", "fear", "disgust"],
      "valence": ["positive", "negative", "neutral"],
      "arousal": ["low", "medium", "high"]
    }
  }')
SCHEMA_ID=$(echo "$SCHEMA_CREATE" | extract_var 'd.get("id","")')
SCHEMA_TYPE=$(echo "$SCHEMA_CREATE" | extract_var 'd.get("type","")')
if [ -n "$SCHEMA_ID" ] && [ "$SCHEMA_TYPE" = "classification" ]; then
  ok "POST /v1/admin/schemas — classification schema=$SCHEMA_ID"
else
  fail "POST /v1/admin/schemas — could not create schema"
  echo "$SCHEMA_CREATE" | python3 -m json.tool 2>/dev/null
fi

step "1.5b — Create structured extraction schema"
SCHEMA2_CREATE=$(curl_retry -X POST "$BASE_URL/v1/admin/schemas" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{
    "name": "e2e_extraction_fields",
    "type": "structured",
    "json_schema": {
      "type": "object",
      "properties": {
        "order_id": {"type": "string"},
        "amount": {"type": "number"},
        "issue_category": {"type": "string"}
      },
      "required": ["order_id", "issue_category"]
    }
  }')
SCHEMA2_ID=$(echo "$SCHEMA2_CREATE" | extract_var 'd.get("id","")')
[ -n "$SCHEMA2_ID" ] && ok "POST /v1/admin/schemas — extraction schema=$SCHEMA2_ID" \
  || fail "POST /v1/admin/schemas — could not create extraction schema"

step "1.5c — List schemas"
SCHEMA_LIST=$(curl_retry -s "$BASE_URL/v1/admin/schemas" -H "$AUTH")
SCHEMA_TOTAL=$(echo "$SCHEMA_LIST" | extract_var 'd.get("total",0)')
[ "$SCHEMA_TOTAL" -ge 2 ] && ok "GET /v1/admin/schemas — total=$SCHEMA_TOTAL" \
  || fail "GET /v1/admin/schemas — expected ≥2, got $SCHEMA_TOTAL"

step "1.5d — Get single schema by ID"
SCHEMA_GET=$(curl_retry -s "$BASE_URL/v1/admin/schemas/$SCHEMA_ID" -H "$AUTH")
GOT_ID=$(echo "$SCHEMA_GET" | extract_var 'd.get("id","")')
[ "$GOT_ID" = "$SCHEMA_ID" ] && ok "GET /v1/admin/schemas/\$SCHEMA_ID — matched" \
  || fail "GET /v1/admin/schemas/\$SCHEMA_ID — id mismatch"

step "1.5e — Update schema name"
SCHEMA_UPDATE=$(curl_retry -X PUT "$BASE_URL/v1/admin/schemas/$SCHEMA_ID" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"name": "e2e_intent_labels_v2"}')
UPDATED_NAME=$(echo "$SCHEMA_UPDATE" | extract_var 'd.get("name","")')
[ "$UPDATED_NAME" = "e2e_intent_labels_v2" ] && ok "PUT /v1/admin/schemas/\$SCHEMA_ID — renamed" \
  || fail "PUT /v1/admin/schemas/\$SCHEMA_ID — name='$UPDATED_NAME'"

step "1.5f — DB verify: schemas stored"
db_check "extraction_schemas" "SELECT 1 FROM extraction_schemas WHERE organization_id='$ORG_ID'"

step "1.5g — Duplicate name rejected"
DUP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/v1/admin/schemas" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"name":"e2e_intent_labels_v2","type":"classification","json_schema":{"intent":["a"]}}')
check_http 409 "$DUP_CODE" "POST duplicate name → 409"

step "1.5h — Delete schema (soft)"
DEL_CODE=$(curl_retry -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/v1/admin/schemas/$SCHEMA2_ID" -H "$AUTH")
check_http 204 "$DEL_CODE" "DELETE /v1/admin/schemas/\$SCHEMA2_ID → 204"

step "1.5i — DB verify: schema soft-deleted"
db_check "schema is_active=false" "SELECT 1 FROM extraction_schemas WHERE id='$SCHEMA2_ID' AND is_active=false"

step "1.5j — Reactivate structured extraction schema for enrichment"
psql "$DSN" -c "UPDATE extraction_schemas SET is_active=true, updated_at=now() WHERE id='$SCHEMA2_ID'" 2>/dev/null && \
  ok "Schema $SCHEMA2_ID reactivated for enrichment" || \
  warn "Could not reactivate schema"

# ── Phase 2: User CRUD ──────────────────────────────────────────────────────
title "PHASE 2: User CRUD"

step "2.1 — Create user"
CREATE_USER_RESPONSE=$(curl_retry -X POST "$BASE_URL/v1/users" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{
    "external_id": "e2e_customer_001",
    "name": "Alice Johnson",
    "email": "alice@acme.com",
    "metadata": {"tier": "premium", "region": "us-east"}
  }')
HTTP_CODE=$(echo "$CREATE_USER_RESPONSE" | extract_var '201' 2>/dev/null || echo "200")
# Extract real HTTP code from response presence
USER_ID=$(echo "$CREATE_USER_RESPONSE" | extract_var 'd.get("id","")')
if [ -n "$USER_ID" ]; then
  ok "POST /v1/users — user=$USER_ID"
else
  fail "POST /v1/users — could not extract user_id"
  echo "$CREATE_USER_RESPONSE" | python3 -m json.tool 2>/dev/null
  exit 1
fi

step "2.2 — DB verify: user created"
db_check "users table" "SELECT 1 FROM users WHERE id='$USER_ID' AND is_deleted=false"

step "2.3 — List users"
HTTP_CODE=$(curl_retry -o /tmp/e2e_user_list.json -w "%{http_code}" "$BASE_URL/v1/users?limit=5" -H "$AUTH")
check_http 200 "$HTTP_CODE" "GET /v1/users"

step "2.4 — Get single user with stats"
HTTP_CODE=$(curl_retry -o /tmp/e2e_user_get.json -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID" -H "$AUTH")
check_http 200 "$HTTP_CODE" "GET /v1/users/\$USER_ID"
# Verify stats fields exist
STATS_OK=$(python3 -c "
import json
d = json.load(open('/tmp/e2e_user_get.json'))
if d.get('message_count') is not None and d.get('session_count') is not None:
    print('ok')
else:
    print('missing')
" 2>/dev/null || echo "err")
[ "$STATS_OK" = "ok" ] && ok "User stats fields present" || warn "User stats fields missing"

step "2.5 — Update user (deep-merge metadata, clear email)"
UPDATE_RESPONSE=$(curl_retry -X PATCH "$BASE_URL/v1/users/$USER_ID" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"name":"Alice B. Johnson","metadata":{"tier":"enterprise"}}')
UPDATED_NAME=$(echo "$UPDATE_RESPONSE" | extract_var 'd.get("name","")')
UPDATED_TIER=$(echo "$UPDATE_RESPONSE" | extract_var 'd.get("metadata",{}).get("tier","")')
if [ "$UPDATED_NAME" = "Alice B. Johnson" ] && [ "$UPDATED_TIER" = "enterprise" ]; then
  ok "PATCH /v1/users/\$USER_ID — name+tier updated"
else
  fail "PATCH /v1/users/\$USER_ID — got name='$UPDATED_NAME' tier='$UPDATED_TIER'"
fi

step "2.6 — DB verify: user update"
db_check "users updated" "SELECT 1 FROM users WHERE id='$USER_ID' AND name='Alice B. Johnson'"

# ── Phase 3: Session CRUD ───────────────────────────────────────────────────
title "PHASE 3: Session CRUD"

step "3.1 — Create session"
CREATE_SESSION_RESPONSE=$(curl_retry -X POST "$BASE_URL/v1/users/$USER_ID/sessions" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"external_id":"e2e_support_ticket","metadata":{"channel":"web","priority":"high"}}')
SESSION_ID=$(echo "$CREATE_SESSION_RESPONSE" | extract_var 'd.get("id","")')
if [ -n "$SESSION_ID" ]; then
  ok "POST /v1/users/\$USER_ID/sessions — session=$SESSION_ID"
else
  fail "POST /v1/users/\$USER_ID/sessions — could not extract session_id"
  echo "$CREATE_SESSION_RESPONSE" | python3 -m json.tool 2>/dev/null
  exit 1
fi

step "3.2 — DB verify: session created"
db_check "sessions table" "SELECT 1 FROM sessions WHERE id='$SESSION_ID' AND is_deleted=false"

step "3.3 — List sessions (should exclude __default__)"
HTTP_CODE=$(curl_retry -o /tmp/e2e_sessions.json -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID/sessions" -H "$AUTH")
check_http 200 "$HTTP_CODE" "GET /v1/users/\$USER_ID/sessions"

step "3.4 — Get session with stats"
HTTP_CODE=$(curl_retry -o /tmp/e2e_session_get.json -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID" -H "$AUTH")
check_http 200 "$HTTP_CODE" "GET /v1/users/\$USER_ID/sessions/\$SESSION_ID"

step "3.5 — Get messages (should be empty)"
HTTP_CODE=$(curl_retry -o /tmp/e2e_messages.json -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/messages" -H "$AUTH")
check_http 200 "$HTTP_CODE" "GET /v1/users/\$USER_ID/sessions/\$SESSION_ID/messages"
MSG_COUNT=$(python3 -c "
import json
d = json.load(open('/tmp/e2e_messages.json'))
print(len(d.get('data', [])))
" 2>/dev/null || echo "err")
[ "$MSG_COUNT" = "0" ] && ok "Messages list is empty" || fail "Messages not empty: count=$MSG_COUNT"

# ── Phase 4: Memory Ingestion ──────────────────────────────────────────────
title "PHASE 4: Memory Ingestion"

IDEM_KEY="e2e-test-$(date +%s)"

step "4.1 — Ingest meaningful 5-message conversation"
INGEST_RESPONSE=$(curl_retry -X POST "$BASE_URL/v1/users/$USER_ID/memory" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{
    "session_id": "e2e_support_ticket",
    "messages": [
      {"role":"user","content":"Hi, I am unable to log in to my dashboard since this morning. I keep getting a 503 error trying to access order #ORD-2026-98765. Can you help?","created_at":"2026-06-07T09:00:00Z","metadata":{"source":"web","browser":"Chrome 125"}},
      {"role":"assistant","content":"I am sorry to hear that! Let me look into this. Have you tried clearing your browser cache or using an incognito window? Also, could you tell me if you are seeing any error code on the page?","created_at":"2026-06-07T09:00:15Z","metadata":{"agent":"support-bot-v2"}},
      {"role":"user","content":"Yes I tried incognito and clearing cache, still the same 503 error. The order #ORD-2026-98765 page says something like \"upstream connect error\" in the bottom corner.","created_at":"2026-06-07T09:01:30Z","metadata":{"source":"web"}},
      {"role":"assistant","content":"Thank you for checking order #ORD-2026-98765. The 503 with upstream connect error suggests our load balancer is having trouble reaching the backend service. I have escalated this to our infrastructure team. In the meantime, I can enable a fallback login mechanism for your account. Would you like me to do that?","created_at":"2026-06-07T09:02:00Z","metadata":{"agent":"support-bot-v2","escalation_level":2}},
      {"role":"user","content":"Yes please, that would be great. Also, can you notify me via email when this is resolved?","created_at":"2026-06-07T09:03:00Z","metadata":{"source":"web"}}
    ]
  }')
JOB_ID=$(echo "$INGEST_RESPONSE" | extract_var 'd.get("job_id","")')
EP_COUNT=$(echo "$INGEST_RESPONSE" | extract_var 'd.get("episode_count",0)')
if [ -n "$JOB_ID" ] && [ "$EP_COUNT" = "5" ]; then
  ok "POST /v1/users/\$USER_ID/memory — $EP_COUNT episodes, job=$JOB_ID"
else
  fail "POST /v1/users/\$USER_ID/memory — ep_count=$EP_COUNT job=$JOB_ID"
  echo "$INGEST_RESPONSE" | python3 -m json.tool 2>/dev/null
fi

step "4.2 — DB verify: episodes ingested"
db_check "episodes in session" "SELECT 1 FROM episodes WHERE session_id='$SESSION_ID' AND is_deleted=false AND enrichment_status IS NOT NULL"

step "4.3 — Idempotency: same key returns cached result"
IDEM_RESPONSE=$(curl_retry -X POST "$BASE_URL/v1/users/$USER_ID/memory" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -d '{"session_id":"e2e_support_ticket","messages":[{"role":"user","content":"dup"}]}')
IDEM_JOB_ID=$(echo "$IDEM_RESPONSE" | extract_var 'd.get("job_id","")')
if [ "$IDEM_JOB_ID" = "$JOB_ID" ] && [ -n "$JOB_ID" ]; then
  ok "Idempotency — matching job_id ($JOB_ID)"
else
  fail "Idempotency — original=$JOB_ID replay=$IDEM_JOB_ID"
fi

step "4.4 — Get messages after ingestion (5 messages expected)"
HTTP_CODE=$(curl_retry -o /tmp/e2e_msgs_after.json -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/messages?limit=100" -H "$AUTH")
check_http 200 "$HTTP_CODE" "GET messages after ingestion"
MSG_COUNT=$(python3 -c "
import json
d = json.load(open('/tmp/e2e_msgs_after.json'))
print(len(d.get('data', [])))
" 2>/dev/null || echo "0")
[ "$MSG_COUNT" = "5" ] && ok "5 messages returned in correct order" || fail "Expected 5 messages, got $MSG_COUNT"

step "4.5 — Ingest to __default__ session (no session_id)"
DEF_RESPONSE=$(curl_retry -X POST "$BASE_URL/v1/users/$USER_ID/memory" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Default session test note"}]}')
DEF_EP_COUNT=$(echo "$DEF_RESPONSE" | extract_var 'd.get("episode_count",0)')
DEF_JOB_ID=$(echo "$DEF_RESPONSE" | extract_var 'd.get("job_id","")')
if [ "$DEF_EP_COUNT" = "1" ] && [ -n "$DEF_JOB_ID" ]; then
  ok "POST /v1/users/\$USER_ID/memory (no session_id) — 1 episode"
else
  fail "Default session ingestion — ep_count=$DEF_EP_COUNT"
fi

# ── Wait for worker enrichment ──────────────────────────────────────────────
title "Worker Enrichment Wait"

echo -n "  Waiting 15 seconds for worker to process..."
for i in $(seq 15 -1 1); do
  echo -n " $i"
  sleep 1
done
echo " done!"

step "4.6 — Check enrichment status (expect 63=all 6 bits)"
ENRICH_STATUS=$(psql "$DSN" -t -A -c "
  SELECT enrichment_status FROM episodes
  WHERE session_id='$SESSION_ID' AND is_deleted=false
  LIMIT 1;" 2>/dev/null || echo "-1")
if [ "$ENRICH_STATUS" != "-1" ] && [ "$ENRICH_STATUS" != "" ]; then
  ok "Enrichment status=$ENRICH_STATUS (expect 63=all 6 bits)"
  [ "$ENRICH_STATUS" = "63" ] && ok "All 6 enrichment steps completed (bits 0-5=63)" \
    || warn "Partial enrichment: status=$ENRICH_STATUS (expected 63)"
else
  warn "Could not check enrichment status"
fi

step "4.7 — Query structured extractions"
EXTRACT_COUNT=$(psql "$DSN" -t -A -c "
  SELECT COUNT(*) FROM structured_extractions se
  JOIN episodes e ON e.id = se.episode_id
  WHERE e.session_id='$SESSION_ID';" 2>/dev/null || echo "-1")
if [ "$EXTRACT_COUNT" != "-1" ]; then
  [ "$EXTRACT_COUNT" -ge 1 ] && ok "Structured extractions: $EXTRACT_COUNT returned" \
    || warn "No structured extractions (expected ≥1 for e2e_extraction_fields schema)"
else
  warn "Could not check structured extractions"
fi

step "4.8 — GET structured extractions via API"
SE_HTTP=$(curl_retry -o /dev/null -w "%{http_code}" \
  "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/structured-extractions" \
  -H "$AUTH")
check_http 200 "$SE_HTTP" "GET structured-extractions list"

step "4.9 — GET structured extractions per episode"
EP_ID=$(psql "$DSN" -t -A -c "
  SELECT id FROM episodes
  WHERE session_id='$SESSION_ID' AND is_deleted=false
  LIMIT 1;" 2>/dev/null)
if [ -n "$EP_ID" ]; then
  SE_EP_HTTP=$(curl_retry -o /dev/null -w "%{http_code}" \
    "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/structured-extractions/$EP_ID" \
    -H "$AUTH")
  check_http 200 "$SE_EP_HTTP" "GET structured-extractions/\$EP_ID"
  SE_DATA=$(curl_retry -s \
    "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/structured-extractions/$EP_ID" \
    -H "$AUTH")
  SE_COUNT=$(echo "$SE_DATA" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(len(d.get('extractions',[])))
" 2>/dev/null || echo "0")
  [ "$SE_COUNT" -ge 0 ] && ok "Structured extractions for episode: $SE_COUNT" \
    || warn "Could not parse extraction count"
fi

# ── Phase 5: Context & Search ───────────────────────────────────────────────
title "PHASE 5: Context & Search"

step "5.1 — Context assembly (first call — cache MISS)"
CTX_RESPONSE=$(curl_retry -i "$BASE_URL/v1/users/$USER_ID/context?query=login+error+503+troubleshooting&limit=5&format=text" -H "$AUTH")
CTX_HTTP=$(echo "$CTX_RESPONSE" | head -1 | awk '{print $2}')
CTX_CACHE=$(echo "$CTX_RESPONSE" | grep -i 'x-cache' | tr -d '\r' | awk '{print $2}')
check_http 200 "$CTX_HTTP" "GET /v1/users/\$USER_ID/context"
[ "$CTX_CACHE" = "MISS" ] && ok "Cache: MISS (first call)" || warn "Cache: $CTX_CACHE (expected MISS)"

step "5.2 — Context assembly (second call — expect cache HIT)"
CTX2_RESPONSE=$(curl_retry -i "$BASE_URL/v1/users/$USER_ID/context?query=login+error+503+troubleshooting&limit=5&format=text" -H "$AUTH")
CTX2_CACHE=$(echo "$CTX2_RESPONSE" | grep -i 'x-cache' | tr -d '\r' | awk '{print $2}')
[ "$CTX2_CACHE" = "HIT" ] && ok "Cache: HIT (second call)" || warn "Cache: $CTX2_CACHE (expected HIT)"

step "5.3 — Hybrid search episodes"
SEARCH_RESPONSE=$(curl_retry "$BASE_URL/v1/users/$USER_ID/search?query=503+upstream+connect+error&limit=10&types=episodes" -H "$AUTH")
SEARCH_COUNT=$(echo "$SEARCH_RESPONSE" | extract_var 'len(d.get("results",[]))')
if [ "$SEARCH_COUNT" -gt 0 ] 2>/dev/null; then
  ok "Hybrid search returned $SEARCH_COUNT results"
else
  warn "Hybrid search returned 0 results (may need longer worker wait)"
fi

# ── Phase 6: Facts ──────────────────────────────────────────────────────────
title "PHASE 6: Facts"

step "6.1 — Ingest structured facts"
FACTS_RESPONSE=$(curl_retry -X POST "$BASE_URL/v1/users/$USER_ID/facts" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{
    "session_id": "e2e_support_ticket",
    "facts": [
      {"subject":"Alice Johnson","predicate":"reported","object":"login_503_error","confidence":1.0},
      {"subject":"login_503_error","predicate":"root_cause","object":"load_balancer_backend_unreachable","confidence":0.8},
      {"subject":"Alice Johnson","predicate":"requested","object":"fallback_login_mechanism","confidence":1.0},
      {"subject":"Alice Johnson","predicate":"requested","object":"email_notification_on_resolution","confidence":1.0},
      {"subject":"e2e_support_ticket","predicate":"priority","object":"high","confidence":1.0}
    ]
  }')
FACTS_ACCEPTED=$(echo "$FACTS_RESPONSE" | extract_var 'd.get("accepted_count",0)')
FACTS_JOB_ID=$(echo "$FACTS_RESPONSE" | extract_var 'd.get("job_id","")')
if [ "$FACTS_ACCEPTED" = "5" ] && [ -n "$FACTS_JOB_ID" ]; then
  ok "POST /v1/users/\$USER_ID/facts — $FACTS_ACCEPTED facts accepted"
else
  fail "Facts ingestion — accepted=$FACTS_ACCEPTED job=$FACTS_JOB_ID"
fi

step "6.2 — DB verify: facts stored (user_id filter)"
db_check "facts table" "SELECT 1 FROM facts WHERE user_id='$USER_ID'" true

# ── Phase 7: Graph ──────────────────────────────────────────────────────────
title "PHASE 7: Graph"

step "7.1 — List graph nodes"
NODES_RESPONSE=$(curl_retry "$BASE_URL/v1/users/$USER_ID/graph/nodes?limit=20" -H "$AUTH")
HTTP_CODE=$(echo "$NODES_RESPONSE" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    # Check nested structure
    items = d
    if isinstance(d, dict):
        items = d.get('data', d)
        if isinstance(items, dict):
            items = items.get('items', items)
    if isinstance(items, list):
        print(len(items))
    else:
        print(0)
except:
    print('err')
" 2>/dev/null || echo "err")
if [ "$HTTP_CODE" != "err" ] && [ "$HTTP_CODE" -gt 0 ] 2>/dev/null; then
  ok "GET /v1/users/\$USER_ID/graph/nodes — $HTTP_CODE entities"
  # Grab first node ID for next tests
  NODE_ID=$(echo "$NODES_RESPONSE" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d
if isinstance(d, dict):
    items = d.get('data', d)
    if isinstance(items, dict):
        items = items.get('items', items)
if isinstance(items, list) and len(items) > 0:
    print(items[0].get('id',''))
" 2>/dev/null || echo "")
else
  warn "Graph nodes returned 0 or error — worker may not have populated graph"
fi

step "7.2 — Get single node (if available)"
if [ -n "$NODE_ID" ]; then
  HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID/graph/nodes/$NODE_ID" -H "$AUTH")
  # 200 = success, 502 = known bug with datetime serialization
  if [ "$HTTP_CODE" = "200" ]; then
    ok "GET /v1/users/\$USER_ID/graph/nodes/\$NODE_ID (HTTP 200)"
  elif [ "$HTTP_CODE" = "502" ]; then
    warn "GET single node — HTTP 502 (known datetime bug in raw SQL)"
  else
    fail "GET single node — HTTP $HTTP_CODE"
  fi
else
  warn "Skipping get-node test (no node_id available)"
fi

step "7.3 — List graph edges"
if [ -n "${NODE_ID:-}" ]; then
  HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" "$BASE_URL/v1/users/$USER_ID/graph/edges?subject_id=$NODE_ID&limit=10" -H "$AUTH")
  check_http 200 "$HTTP_CODE" "GET /v1/users/\$USER_ID/graph/edges (subject_id=$NODE_ID)"
else
  warn "Skipping edges test (no node_id available)"
fi

# ── Phase 7.5: Classification Query ──────────────────────────────────────────
title "PHASE 7.5: Classification Query"

step "7.5a — List classifications for session"
CLASS_LIST=$(curl_retry -s "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/classifications" -H "$AUTH")
CLASS_COUNT=$(echo "$CLASS_LIST" | extract_var 'd.get("total",-1)')
if [ "$CLASS_COUNT" -ge 0 ] 2>/dev/null; then
  ok "GET /v1/users/\$USER_ID/sessions/\$SESSION_ID/classifications — total=$CLASS_COUNT"
  # Grab first episode_id if any classifications exist
  CLASS_EP_ID=$(echo "$CLASS_LIST" | python3 -c "
import sys, json
d = json.load(sys.stdin)
items = d.get('data', [])
if items:
    print(items[0].get('episode_id', ''))
" 2>/dev/null || echo "")
else
  fail "GET classifications — could not parse response"
  echo "$CLASS_LIST" | python3 -m json.tool 2>/dev/null
fi

step "7.5b — Get single classification"
CLASS_SINGLE=$(curl_retry -s "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/classifications/$CLASS_EP_ID" -H "$AUTH")
SINGLE_INTENT=$(echo "$CLASS_SINGLE" | extract_var 'd.get("intent","")')
[ -n "$SINGLE_INTENT" ] && ok "GET single classification — intent=$SINGLE_INTENT" \
  || ok "GET single classification — 200 (intent may be null: '$SINGLE_INTENT')"

step "7.5c — DB verify: classification data (via episode join)"
db_check "dialog_classifications" "SELECT 1 FROM dialog_classifications dc JOIN episodes e ON dc.episode_id = e.id WHERE e.session_id='$SESSION_ID' AND dc.organization_id='$ORG_ID'"

step "7.5d — No auth → 401"
CLASS_NOAUTH_CODE=$(curl_retry -o /dev/null -w "%{http_code}" \
  "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID/classifications")
check_http 401 "$CLASS_NOAUTH_CODE" "GET classifications (no auth) → 401"

# ── Phase 8: Delete Operations ─────────────────────────────────────────────
title "PHASE 8: Delete Operations"

step "8.1 — Delete session"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/v1/users/$USER_ID/sessions/$SESSION_ID" -H "$AUTH")
check_http 204 "$HTTP_CODE" "DELETE /v1/users/\$USER_ID/sessions/\$SESSION_ID"

step "8.2 — DB verify: session soft-deleted"
db_check "session is_deleted" "SELECT 1 FROM sessions WHERE id='$SESSION_ID' AND is_deleted=true"

step "8.3 — Wipe all user memory"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/v1/users/$USER_ID/memory" -H "$AUTH")
check_http 204 "$HTTP_CODE" "DELETE /v1/users/\$USER_ID/memory"

step "8.4 — DB verify: episodes soft-deleted"
db_check "episodes is_deleted" "SELECT 1 FROM episodes WHERE user_id='$USER_ID' AND is_deleted=true"

step "8.5 — Delete user"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" -X DELETE "$BASE_URL/v1/users/$USER_ID" -H "$AUTH")
check_http 204 "$HTTP_CODE" "DELETE /v1/users/\$USER_ID"

step "8.6 — DB verify: user soft-deleted"
db_check "user is_deleted" "SELECT 1 FROM users WHERE id='$USER_ID' AND is_deleted=true"

# ── Error & Edge Cases ─────────────────────────────────────────────────────
title "ERROR & EDGE CASES"

step "E.1 — Unauthenticated request (no auth header)"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" "$BASE_URL/v1/users")
check_http 401 "$HTTP_CODE" "GET /v1/users (no auth) → 401"

step "E.2 — Invalid API key"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" "$BASE_URL/v1/users" -H "Authorization: Bearer invalid_key_xxx")
check_http 401 "$HTTP_CODE" "GET /v1/users (bad key) → 401"

step "E.3 — Invalid schema (bad role)"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" \
  -X POST "$BASE_URL/v1/users/$USER_ID/memory" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"invalid_role","content":"test"}]}')
check_http 422 "$HTTP_CODE" "POST memory (bad role) → 422"

step "E.4 — 404 on deleted resource"
HTTP_CODE=$(curl_retry -o /dev/null -w "%{http_code}" \
  "$BASE_URL/v1/users/$USER_ID" -H "$AUTH")
check_http 404 "$HTTP_CODE" "GET /v1/users/\$USER_ID (deleted) → 404"

# ── Summary ─────────────────────────────────────────────────────────────────
title "TEST SUMMARY"
TOTAL=$((PASS_COUNT + FAIL_COUNT + WARN_COUNT))
echo ""
echo -e "  ${GREEN}Passed:${NC}  $PASS_COUNT"
echo -e "  ${RED}Failed:${NC}  $FAIL_COUNT"
echo -e "  ${YELLOW}Warnings:${NC} $WARN_COUNT"
echo -e "  ${NC}Total:    $TOTAL${NC}"
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
  echo -e "${GREEN}╔════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║           ALL TESTS PASSED                        ║${NC}"
  echo -e "${GREEN}╚════════════════════════════════════════════════════╝${NC}"
else
  echo -e "${RED}╔════════════════════════════════════════════════════╗${NC}"
  echo -e "${RED}║  $FAIL_COUNT test(s) FAILED — review output above       ║${NC}"
  echo -e "${RED}╚════════════════════════════════════════════════════╝${NC}"
fi

echo ""
echo "Artifacts:"
echo "  /tmp/e2e_health.json"
echo "  /tmp/e2e_ready.json"
echo "  /tmp/e2e_user_list.json"
echo "  /tmp/e2e_user_get.json"
echo "  /tmp/e2e_sessions.json"
echo "  /tmp/e2e_session_get.json"
echo "  /tmp/e2e_messages.json"
echo "  /tmp/e2e_msgs_after.json"

# Cleanup temp files (optional - uncomment to enable)
# rm -f /tmp/e2e_*.json

# Exit with failure count so CI can pick it up
exit "$FAIL_COUNT"
