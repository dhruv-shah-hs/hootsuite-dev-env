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

