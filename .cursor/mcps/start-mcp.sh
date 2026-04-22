#!/usr/bin/env bash
# Load MCP environment variables for Cursor from repo .env.
#
# Required in .env:
#   JIRA_INSTANCE_URL, JIRA_USER_EMAIL, JIRA_API_KEY
#
# Optional:
#   JIRA_ENV_FILE  explicit path to env file
#                  (default: nearest .env walking up from current directory)
#
# Optional (GitHub MCP):
#   GITHUB_PERSONAL_ACCESS_TOKEN  PAT for GitHub MCP server
#   GITHUB_HOST                  GitHub Enterprise host, e.g. https://github.example.com
#
# Usage (from repo root, or any subdirectory):
#   source ./.cursor/external_sources/start-mcp.sh
#   open -a Cursor
#
# One-liner:
#   eval "$(./.cursor/external_sources/start-mcp.sh --print-eval)"
#
# Works when sourced from bash or zsh (shebang is ignored when sourcing).
#
set -euo pipefail

# Resolve .env without BASH_SOURCE (not set when zsh sources this file).
resolve_env_file() {
  if [ -n "${JIRA_ENV_FILE:-}" ]; then
    printf '%s\n' "$JIRA_ENV_FILE"
    return
  fi
  local _d
  _d="$(pwd -L 2>/dev/null || pwd)"
  while [ "$_d" != "/" ]; do
    if [ -f "$_d/.env" ]; then
      printf '%s\n' "$_d/.env"
      return
    fi
    _d="$(dirname "$_d")"
  done
  printf '%s\n' ""
}

ENV_FILE="$(resolve_env_file)"

die() {
  echo "$*" >&2
  exit 1
}

# True if this file was sourced (not executed in a new shell).
is_sourced() {
  if [ -n "${BASH_SOURCE[0]:-}" ]; then
    [ "${BASH_SOURCE[0]}" != "$0" ]
    return
  fi
  if [ -n "${ZSH_VERSION:-}" ]; then
    case ${ZSH_EVAL_CONTEXT:-} in *:file*) return 0 ;; esac
  fi
  return 1
}

load_from_env() {
  [ -n "$ENV_FILE" ] || die "No .env found (set JIRA_ENV_FILE or run from inside the repo)."

  [ -f "$ENV_FILE" ] || die "Missing ${ENV_FILE}. Create it (e.g. copy .env.example) and set JIRA_INSTANCE_URL, JIRA_USER_EMAIL, JIRA_API_KEY."

  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a

  [ -n "${JIRA_INSTANCE_URL:-}" ] && [ -n "${JIRA_USER_EMAIL:-}" ] && [ -n "${JIRA_API_KEY:-}" ] || \
    die "Set non-empty JIRA_INSTANCE_URL, JIRA_USER_EMAIL, and JIRA_API_KEY in ${ENV_FILE}."

  export JIRA_INSTANCE_URL JIRA_USER_EMAIL JIRA_API_KEY

  # GitHub MCP: optional, but if any GitHub-related variable is set, validate basics.
  if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ] || [ -n "${GITHUB_HOST:-}" ]; then
    [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ] || die "Set non-empty GITHUB_PERSONAL_ACCESS_TOKEN in ${ENV_FILE} to use GitHub MCP."
    if [ -n "${GITHUB_HOST:-}" ]; then
      case "$GITHUB_HOST" in
        https://*) ;;
        http://*) die "GITHUB_HOST must use https:// for GitHub Enterprise (got http://...). Update ${ENV_FILE}." ;;
        *) die "GITHUB_HOST must include scheme (https://...). Update ${ENV_FILE}." ;;
      esac
      export GITHUB_HOST
    fi
    export GITHUB_PERSONAL_ACCESS_TOKEN
  fi

  echo "Loaded Jira MCP variables from ${ENV_FILE}" >&2
}

print_eval() {
  load_from_env
  printf 'export JIRA_INSTANCE_URL=%q\n' "$JIRA_INSTANCE_URL"
  printf 'export JIRA_USER_EMAIL=%q\n' "$JIRA_USER_EMAIL"
  printf 'export JIRA_API_KEY=%q\n' "$JIRA_API_KEY"
  if [ -n "${GITHUB_PERSONAL_ACCESS_TOKEN:-}" ]; then
    printf 'export GITHUB_PERSONAL_ACCESS_TOKEN=%q\n' "$GITHUB_PERSONAL_ACCESS_TOKEN"
  fi
  if [ -n "${GITHUB_HOST:-}" ]; then
    printf 'export GITHUB_HOST=%q\n' "$GITHUB_HOST"
  fi
}

case "${1:-}" in
  --print-eval)
    print_eval
    ;;
  -h|--help)
    sed -n '2,/^$/p' "$0" | sed 's/^# //' >&2
    exit 0
    ;;
  "")
    if ! is_sourced; then
      die "Do not execute this script directly (exports would be lost in a subshell).

  source ./.cursor/external_sources/start-mcp.sh
Or:
  eval \"\$(./scripts/jira-mcp-env.sh --print-eval)\"
"
    fi
    load_from_env
    ;;
  *)
    die "Unknown option: $1 (use --print-eval or source without args)"
    ;;
esac
