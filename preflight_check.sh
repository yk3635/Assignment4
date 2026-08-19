#!/usr/bin/env bash

#export GITLAB_URL="https://gitlab.url"
#export GITLAB_TOKEN="glpat-xxxxxxxxxxxxxxxxxxxx"
#export GITLAB_NAMESPACE="ops-validation"
#export EXPECTED_VERSION="19.2.4"

# preflight_check.sh
# Validates GITLAB_URL, GITLAB_TOKEN, GITLAB_NAMESPACE, EXPECTED_VERSION
# are set correctly before running gitlab_upgrade_validation.py
#
# Usage: bash preflight_check.sh

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; FAILED=1; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

FAILED=0

echo "=== GitLab Validation Preflight Check ==="
echo ""

# --- GITLAB_URL ---
if [[ -z "${GITLAB_URL:-}" ]]; then
    fail "GITLAB_URL is not set"
else
    if [[ "$GITLAB_URL" =~ ^https?:// ]]; then
        pass "GITLAB_URL is set: $GITLAB_URL"
    else
        fail "GITLAB_URL doesn't start with http:// or https:// -> '$GITLAB_URL'"
    fi
    if [[ "$GITLAB_URL" == */ ]]; then
        warn "GITLAB_URL has a trailing slash — remove it to avoid double-slash API URLs"
    fi
fi

# --- GITLAB_TOKEN ---
if [[ -z "${GITLAB_TOKEN:-}" ]]; then
    fail "GITLAB_TOKEN is not set"
elif [[ "$GITLAB_TOKEN" == "glpat-xxxxxxxxxxxxxxxxxxxx" ]]; then
    fail "GITLAB_TOKEN still has the placeholder value from the example — set a real token"
else
    pass "GITLAB_TOKEN is set (${#GITLAB_TOKEN} chars, starts with '${GITLAB_TOKEN:0:6}...')"
fi

# --- GITLAB_NAMESPACE ---
if [[ -z "${GITLAB_NAMESPACE:-}" ]]; then
    warn "GITLAB_NAMESPACE not set — project will be created under the token owner's personal namespace"
else
    pass "GITLAB_NAMESPACE is set: $GITLAB_NAMESPACE"
fi

# --- EXPECTED_VERSION ---
if [[ -z "${EXPECTED_VERSION:-}" ]]; then
    warn "EXPECTED_VERSION not set — version check will be informational only, not enforced"
else
    if [[ "$EXPECTED_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        pass "EXPECTED_VERSION is set: $EXPECTED_VERSION"
    else
        warn "EXPECTED_VERSION '$EXPECTED_VERSION' doesn't look like X.Y.Z — double check it"
    fi
fi

echo ""
echo "--- Live checks against GitLab (requires curl) ---"
echo ""

if [[ -z "${GITLAB_URL:-}" || -z "${GITLAB_TOKEN:-}" ]]; then
    fail "Skipping live checks — GITLAB_URL and/or GITLAB_TOKEN missing"
else
    # Reachability
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "${GITLAB_URL}/-/health" 2>/dev/null)
    if [[ "$HTTP_CODE" == "200" ]]; then
        pass "GitLab instance reachable (/-/health -> 200)"
    else
        fail "GitLab instance not reachable or unhealthy (/-/health -> HTTP $HTTP_CODE)"
    fi

    # Token auth
    USER_RESP=$(curl -s --max-time 10 -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" "${GITLAB_URL}/api/v4/user")
    USERNAME=$(echo "$USER_RESP" | grep -o '"username":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [[ -n "$USERNAME" ]]; then
        pass "Token authenticates successfully as user: $USERNAME"
    else
        fail "Token authentication failed — response: $(echo "$USER_RESP" | head -c 200)"
    fi

    # Token scope check (does it have api scope?)
    PAT_RESP=$(curl -s --max-time 10 -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" "${GITLAB_URL}/api/v4/personal_access_tokens?state=active")
    if echo "$PAT_RESP" | grep -q '"scopes"'; then
        SCOPES=$(echo "$PAT_RESP" | grep -o '"scopes":\[[^]]*\]' | head -1)
        if echo "$SCOPES" | grep -q '"api"'; then
            pass "Token has 'api' scope"
        else
            warn "Could not confirm 'api' scope in: $SCOPES (some checks in the main script will fail without it)"
        fi
    else
        warn "Could not read token scopes (may need admin rights, or this is an impersonation token) — verify manually"
    fi

    # Namespace resolution
    if [[ -n "${GITLAB_NAMESPACE:-}" ]]; then
        NS_RESP=$(curl -s --max-time 10 -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" \
            "${GITLAB_URL}/api/v4/namespaces/${GITLAB_NAMESPACE}")
        NS_ID=$(echo "$NS_RESP" | grep -o '"id":[0-9]*' | head -1 | cut -d':' -f2)
        NS_KIND=$(echo "$NS_RESP" | grep -o '"kind":"[^"]*"' | head -1 | cut -d'"' -f4)
        if [[ -n "$NS_ID" ]]; then
            if [[ "$NS_KIND" == "group" ]]; then
                pass "GITLAB_NAMESPACE '$GITLAB_NAMESPACE' resolves to a group (id=$NS_ID)"
            else
                warn "GITLAB_NAMESPACE '$GITLAB_NAMESPACE' resolves to kind='$NS_KIND' (id=$NS_ID) — expected 'group', double check this isn't a project path"
            fi
        else
            fail "GITLAB_NAMESPACE '$GITLAB_NAMESPACE' does not resolve to a valid namespace — check spelling/permissions (response: $(echo "$NS_RESP" | head -c 150))"
        fi
    fi

    # Version check
    if [[ -n "${EXPECTED_VERSION:-}" ]]; then
        VER_RESP=$(curl -s --max-time 10 -H "PRIVATE-TOKEN: ${GITLAB_TOKEN}" "${GITLAB_URL}/api/v4/version")
        ACTUAL_VER=$(echo "$VER_RESP" | grep -o '"version":"[^"]*"' | head -1 | cut -d'"' -f4)
        if [[ "$ACTUAL_VER" == "$EXPECTED_VERSION"* ]]; then
            pass "Instance version matches: $ACTUAL_VER"
        elif [[ -n "$ACTUAL_VER" ]]; then
            warn "Instance reports version '$ACTUAL_VER', expected '$EXPECTED_VERSION' — is the upgrade actually complete?"
        else
            fail "Could not read version from instance"
        fi
    fi

    # git binary present (needed for the HTTPS clone/push/pull tests)
    if command -v git &>/dev/null; then
        pass "git binary found ($(git --version))"
    else
        fail "git binary not found on PATH — required for the git clone/push/pull tests in the main script"
    fi

    # python requests module
    if python3 -c "import requests" 2>/dev/null; then
        pass "python3 'requests' module available"
    else
        fail "python3 'requests' module missing — run: pip install requests --break-system-packages"
    fi
fi

echo ""
if [[ "$FAILED" -eq 1 ]]; then
    echo -e "${RED}=== Preflight FAILED — fix the items above before running gitlab_upgrade_validation.py ===${NC}"
    exit 1
else
    echo -e "${GREEN}=== Preflight PASSED — safe to run gitlab_upgrade_validation.py ===${NC}"
    exit 0
fi
