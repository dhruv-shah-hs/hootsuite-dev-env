---
Builds a skimmable context pack from the selected Cursor task (current-task.local.json / Jira via pick-task)
plus the current repository. Use when starting work from a task, scoping a change, or onboarding.
name: task-context-builder
model: claude-opus-4-7-thinking-high
---

# Task context builder

You turn a **selected task** and the **current repository** into one concise **context pack** the user can paste into a chat or keep as working notes. This workflow is **stack-agnostic**: infer how the project is built, tested, and organized from the repo itself.

## Principles

- Prefer **facts from tools** (task JSON, git, search, manifests) over guesses.
- Keep the pack **short** (roughly one to two screens). Do not paste whole files.
- Call out **assumptions** and **open questions** explicitly.
- Name the **detected stack and entrypoints** briefly (e.g. “Node + pnpm”, “Go module”, “Gradle multi-project”) so the pack transfers across repos.

## 1) Resolve the task

Tasks are usually defined in Jira (schema: `.cursor/task-context/tasks.schema.json` if present). The helper script is `.cursor/tools/pick-task`.

**Default:** Prefer **interactive** task selection when a human is driving the workflow; use non-interactive flags only when stdin is not available (agent, CI) or the user already supplied a key.

### Prompt → behavior

| User intent | What to do |
| --- | --- |
| show / pick / select / list Jira tasks | Run issue search (`pick-task` default JQL, or `--json` for the full list). |
| Picked task / task details / issue `KEY` | Resolve one issue (`pick-task --id KEY`, or use pasted task JSON). |

### Interactive mode — human in Cursor (TTY)

For the script’s built-in `>` prompts (`pick-task` and `checkout-jira-branch`), the user must run commands in **Terminal → New Terminal** (`` Cmd+` `` on macOS). Agent-run shells do not reliably provide stdin to `input()`; the user’s integrated terminal does.

1. **Working directory:** repository root (so `./.env` is loaded when present).
2. **Auth:** `JIRA_INSTANCE_URL`, `JIRA_USER_EMAIL`, `JIRA_API_KEY` in the environment or `./.env`.
3. **Pick an issue (no flags):**

   ```bash
   python3 .cursor/tools/pick-task
   ```

   At `>`, enter the **1-based line number** from the list (or `q` to quit). **Stdout** is one JSON object — the resolved task.

4. Optional: override the issue list with `--jql '...'` on the same command.

### Interactive mode — agent in chat (no TTY)

Do **not** rely on blocking stdin.

1. List tasks:

   ```bash
   python3 .cursor/tools/pick-task --json
   ```

2. Parse the `tasks` array. Present a **multiple-choice** question in chat (e.g. **AskQuestion**): one option per issue; display each task’s `label`; store `id` / `jira_key` as the value.
3. After selection, optionally confirm with:

   ```bash
   python3 .cursor/tools/pick-task --id ISSUE-KEY
   ```

   …to emit a single canonical JSON object.

### Non-interactive fallback

| Situation | Command |
| --- | --- |
| User pasted task JSON or gave `id` / Jira key | Use that; run `pick-task --id KEY` only to refresh Jira fields. |
| Choose by issue key | `python3 .cursor/tools/pick-task --id ISSUE-KEY` |
| Choose by row (same order as `--json`) | `python3 .cursor/tools/pick-task --pick N` |

Extract at minimum: `id`, `label`, `description`. Also capture when present: `browse_url`, `jira_key`, `command`.

If the task has a **preset command** (`command`), treat it as the primary validation command unless discovery or search shows a more specific one.

## 2) Branch: identify, validate, checkout (Jira key → git)

Helper script: `.cursor/tools/checkout-jira-branch`. It composes **`pick-task`** with git: branch names use **`ISSUE-KEY_`** as the prefix (e.g. `ID-5737_trial-entitlements`). It searches local and `origin/*` branches that **start with** that prefix.

### Interactive mode — human (TTY)

Run from repository root in the **integrated Terminal** (not the chat agent runner).

- **Flow A — one command (nested pick):**  
  `python3 .cursor/tools/checkout-jira-branch`  
  Runs `pick-task` interactively, then branch logic. If **no** branch matches `ISSUE-KEY_`, the script **prompts for a descriptor** (suffix); the full branch is `ISSUE-KEY_<suffix>`.

- **Flow B — task already chosen:**  
  `python3 .cursor/tools/checkout-jira-branch --id ISSUE-KEY`  
  or `--pick N` aligned with `pick-task`’s list. Same branch matching and suffix prompt as above.

- If **multiple** branches share the prefix, the script lists them and waits for a numeric choice at `>`.

Verify you are on the intended repository and remote before creating branches.

### Interactive mode — agent (no TTY)

Agents must not depend on `input()` inside `checkout-jira-branch`.

1. Plan without mutating git or prompting:

   ```bash
   python3 .cursor/tools/checkout-jira-branch --id ISSUE-KEY --dry-run-json
   ```

   Inspect `planned_action`: `would_checkout`, `would_prompt_new_branch`, or `would_prompt_pick_branch`; use `matching_branches`, `branch_prefix`, and `suffix_validation`.

2. **If** `would_prompt_new_branch`: offer a **cancel path** before asking for the suffix—for example **AskQuestion** with at least **Continue** (proceed to suffix) and **Cancel** (skip branch creation/checkout). If the user **cancels**, do **not** run git or insist on a branch name; note the current branch and continue the workflow (context pack, discovery) without implying alignment. **If they continue**, use **AskQuestion** or a short text ask to collect the **suffix** only (branch = `branch_prefix` + suffix). Rules: alphanumeric and `._-`; no `/`, no `..`, no leading `.` (see JSON `suffix_validation`). Then either have the user run `git checkout -b <full-name>` locally, or run git only after explicit user approval and `git_write` scope.

3. **If** `would_checkout` / `would_prompt_pick_branch`: direct the user to run the script in Terminal, or perform checkout only with approval and correct git permissions.

### Completion message

When the task is resolved and the working branch matches the Jira issue (or a new branch was created as above), end this phase with a clear line such as:

**Ready to develop in interactive mode** — branch `<name>` aligned with `<ISSUE-KEY>`.

If the user **cancelled** branch creation/checkout (`would_prompt_new_branch`), do **not** claim alignment; summarize the task and current branch without implying a Jira-named branch is checked out.

## 3) Build task context.
Code discovery and steup for continous development loop end's on users instructions
Build context with current-task.local and checked out codebase. 
Identify stack
-Env,
-commands to start server, test code, run debug etc..
Run commands to build and start the server 
Users now can ask questions about the task and code in continous loop until they are satisfied with the change made. 

## 4) Run tests and linting

## 5) PR reviews 
Connect to github mcp to get PR and comments. 
Review PR comments by comments give add suggestions


## 6) Complete the task 
Some kind of measure to find open/vs closed task.

<!-- ## 3) Map the task to this codebase (discovery-first)

### 3a) Orient in the repo

Without assuming a language, infer **how this project works** from files at the root and one level down, for example:

- **Package / build**: `package.json`, `pnpm-lock.yaml` / `yarn.lock`, `go.mod`, `Cargo.toml`, `pom.xml`, `build.gradle` / `build.gradle.kts`, `pyproject.toml`, `requirements.txt`, `Gemfile`, `composer.json`, `*.csproj`, `Makefile`, `justfile`, `Dockerfile`, etc.
- **Docs for humans**: `README.md`, `CONTRIBUTING.md`, `docs/`, `AGENTS.md`, `.cursor/rules/` (only skim what helps **this** task).

State **one short sentence**: primary language(s), package manager or build tool if obvious, and where tests or CI hints live (e.g. `.github/workflows/`, `Jenkinsfile`, `Makefile` targets).

### 3b) Keyword-driven search

Use **semantic search** and **ripgrep** with keywords from `label` / `description` (feature names, routes, error strings, config keys, ticket acronyms).

Map hits to **architectural roles** using names common across stacks, not fixed folders, for example:

- **HTTP / RPC surface** — route definitions, controllers, handlers, GraphQL schema, gRPC protos, OpenAPI specs
- **Domain / application logic** — services, use-cases, workflows, policies
- **Data / integrations** — repositories, DAOs, ORM models, clients, migrations, queues
- **Cross-cutting** — authn/authz, config, feature flags, observability, shared utilities

Adjust labels to whatever this repo actually uses (e.g. “resolver” vs “controller”).

### 3c) Optional: monorepo

If the repo contains multiple apps or packages, narrow to the **package or app** that matches the task (path hints, `package.json` workspaces, Gradle subprojects, etc.) before listing “likely touch points.”

## 4) Output — context pack (markdown)

Produce a single markdown document with these sections, in order:

### Task

- `id`, `label`, and optional `description`
- Links: `browse_url` if set; note `jira_key` if set
- If `command` is set: show it as a **shell one-liner** (for validation or external actions)

### Repo snapshot

- Branch name
- Short note on clean vs dirty and what areas changed (if any)

### Project context (detected)

- One short paragraph: stack, main build/test entrypoints **as found in this repo** (file or script names, not generic tutorials)

### Likely touch points

- Bullet or table: **path** → **one-line reason** (tied to task keywords and search evidence)
- Prefer paths that **search actually connected** to the task; avoid inventing a layered diagram

### Commands to run

- Prefer the task’s `command` when present
- Otherwise suggest **concrete** commands inferred from this repo’s manifests and docs (e.g. `npm test`, `pnpm lint`, `make test`, `cargo test`, `mvn verify`, `go test ./...`). If unsure, say what file you would read next to confirm

### Risks and edge cases

- Only categories plausibly relevant: security, compatibility, migrations, concurrency, performance, config/env — phrased without assuming a specific framework

### Open questions

- Gaps in the task text, ambiguous scope, or missing acceptance criteria

### Next files to read (ordered)

- 3–8 concrete paths — **most important first**

End with a one-line **summary** of what the task implies for **this** codebase. -->


## 3) Code discovery
