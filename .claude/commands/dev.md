---
description: "Start the FastAPI dev server and verify it responds"
---

1. Check if a uvicorn process is already running on port 8000:
   - `lsof -i:8000` — if something is listening, report it and stop here so the user can decide whether to kill it
2. If port is free, start the dev server in the background:
   - `.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000`
   - Use the Bash tool's `run_in_background: true` parameter
3. Wait for the server to come up (poll until `curl -sf http://127.0.0.1:8000/` returns 200 — use Monitor with `until` if available, otherwise a brief sleep + retry, max 10s)
4. Smoke-test endpoints:
   - `GET /` — should return 200 with HTML
   - `GET /state` — should return 200 with JSON
5. Report:
   - The URL: http://127.0.0.1:8000
   - The background process ID so the user can kill it later
   - Any startup errors visible in the server output

Do NOT kill an existing server without explicit user permission.
