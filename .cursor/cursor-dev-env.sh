#!/usr/bin/env bash
# Load hootsuite-dev-env .env into the current shell, then run interactive Docker
# and Vault logins. Must be sourced so exports and login side effects apply here.
#
#   source ./.cursor/cursor-dev-env.sh
#
# Optional: JIRA_ENV_FILE=/absolute/path/to/.env (overrides .env discovery)
# Optional: CURSOR_DEV_ENV_SKIP_LOGINS=1 to only load .env
#
# Intentionally no "set -e" here: when sourced, that would change the parent shell.

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

die() {
  echo "$*" >&2
  if is_sourced; then
    return 1
  fi
  exit 1
}

if ! is_sourced; then
  die "This script must be sourced (so .env is loaded into your current shell).

  source ./.cursor/cursor-dev-env.sh
"
fi

ENV_FILE="$(resolve_env_file)"

[ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || die "No .env found (set JIRA_ENV_FILE or cd into hootsuite-dev-env with a .env)."

# shellcheck disable=SC1090
set -a
. "$ENV_FILE" || die "Failed to source ${ENV_FILE}."
set +a

echo "Loaded environment from ${ENV_FILE}" >&2

if [ "${CURSOR_DEV_ENV_SKIP_LOGINS:-}" = "1" ]; then
  echo "CURSOR_DEV_ENV_SKIP_LOGINS=1: skipping hootctl and vaultlogin." >&2
  return 0
fi

if ! command -v hootctl &>/dev/null; then
  die "hootctl not found on PATH. Install hootctl / fix PATH, then re-run this script."
fi

if ! command -v vaultlogin &>/dev/null; then
  die "vaultlogin not found on PATH. Install or fix PATH, then re-run this script."
fi

echo "Next: hootctl login docker (password prompt)…" >&2
hootctl login docker || die "hootctl login docker failed."

echo "Next: vaultlogin dev (password prompt)…" >&2
vaultlogin dev || die "vaultlogin dev failed."

echo "Cursor dev environment ready (env loaded, Docker and Vault logins run)." >&2
