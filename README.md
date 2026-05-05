# cursor-dev-env
Hootsuite's development environment to build tech of tomorrow. 
Use guided & measurable AI to build tools and technology.
Only optimized to work with cursor currently.

Steps
1. Pull the repository.
2. Create `.env` from `.env.example` and fill in the variables. Do not commit real secrets.
3. Before a Cursor session (ideally in the same terminal you use for the workspace), load the environment and complete interactive logins. From the **hootsuite-dev-env** repo root:

   ```bash
   source ./.cursor/cursor-dev-env.sh
   ```

   This script sources the nearest `.env` (walking up from your current directory), then runs `hootctl login docker` and `vaultlogin dev`, each of which will prompt for credentials. To only load `.env` without the logins: `CURSOR_DEV_ENV_SKIP_LOGINS=1 source ./.cursor/cursor-dev-env.sh`
4. Open the workspace in Cursor and use agents with `@agent-name` and your prompt.

Following agents avaiable currently

**resolve-task**
Pick one Jira issue (pick-task), persist it to .cursor/context/current-task.local.json (save-task-context). If the ticket is Done (is_deployed), ask whether to continue before saving.

**align-branch**
Point the service repo at a branch whose name starts with JIRA-KEY_ (checkout-jira-branch.py), enforce task.repository vs origin when set. Aligns branch based on the Jira_id or create a new branch based on the renovate naming convention.

**resolve-service**
Run resolve-service.py to generate .cursor/context/service-context.json (stack, Make targets, task_repo_fit, etc.) and refresh VS Code “Attach: service” in launch.json. Run after task + branch alignment when you want a fresh agent context pack.

**start-service**
Read service-context.json, run primary_commands.run (or tell the user the command), surface ports/endpoints/debug hints, optionally background the dev server. 

**build-task-context**
Read current-task.local.json, explore the service repo, and write a status-driven plan (development vs PR review vs QA vs closure) based on task.status. Plan-only deliverable.
Typical order: resolve-task → align-branch → resolve-service → start-service → build-task-context. 

