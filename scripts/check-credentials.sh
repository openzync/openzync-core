#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# OpenZep — Pre-commit credential leak checker
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
err()  { echo -e "${RED}[FAIL]${NC} $*"; }
ok()   { echo -e "${GREEN}[PASS]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }

# Files allowed to contain placeholder credentials
ALLOWED_PATTERNS=("^.env.example$" "^infra/docker-compose" "^docs/" "^backend/.venv/")

is_allowed() {
    local f="$1"
    for p in "${ALLOWED_PATTERNS[@]}"; do [[ "$f" =~ $p ]] && return 0; done
    return 1
}

# Get files: staged (default) or all (--all)
if [ "${1:-}" = "--all" ]; then
    # Don't use find on the full tree — too slow. Rely on pre-commit's staged files.
    ok "Use 'pre-commit run check-credentials' for full scan."
    exit 0
fi

files=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null || true)
[ -z "$files" ] && ok "No staged files." && exit 0

exit_code=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    [ ! -f "$f" ] && continue
    is_allowed "$f" && continue

    # Check for inline credential assignment: VAR=<literal> (not $var)
    if grep -nE '^[^#]*[A-Z]+_(PASSWORD|SECRET|KEY|TOKEN|SECRET_KEY)[[:space:]]*=[[:space:]]*[A-Za-z0-9]' "$f" | \
       grep -vE '\$[A-Z_{(' | grep -q .; then
        err "$f — potential hardcoded credential"
        grep -nE '^[^#]*[A-Z]+_(PASSWORD|SECRET|KEY|TOKEN|SECRET_KEY)[[:space:]]*=[[:space:]]*[A-Za-z0-9]' "$f" | \
          grep -vE '\$[A-Z_{((' | while IFS=: read -r ln rest; do
            warn "  ${f}:${ln} → use an env var instead"
        done
        exit_code=1
    fi
done <<< "$files"

[ "$exit_code" -eq 0 ] && ok "No hardcoded credentials detected."
exit "$exit_code"
