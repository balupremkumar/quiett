---
name: restart-app
description: Cleanly restart the Voice Dictation app. Use after ANY code change to this project, before claiming a fix works, or when the user reports the app behaving as if running old code. Kills all stale processes (pythonw main.py copies and the project's whisper-server.exe), relaunches hidden, confirms the new PID and start time, and tails app.log.
---

# Restart Voice Dictation cleanly

Run:

```powershell
powershell -ExecutionPolicy Bypass -File "D:\Dev\ai\projects\active\quiett\scripts\restart_app.ps1"
```

The script prints which stale PIDs were killed, the new PID with its start timestamp, and the last 15 lines of app.log.

Rules:

- Never restart by just launching run.bat or launch.vbs directly; that leaves stale copies running old code, which has repeatedly caused "the fix didn't work" false alarms.
- After the restart, verify the fix by exercising the changed flow (dictate, check the log), not by the restart alone.
- If the script prints FAIL, read the tailed log before attempting anything else.
- LM Studio is intentionally NOT touched by this script; the Fitness Pal model load is user-toggled.
