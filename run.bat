@echo off
rem Launch Quiett with no console window (pythonw). The app lives in the system tray.
cd /d D:\Dev\ai\projects\active\quiett
start "" ".venv\Scripts\pythonw.exe" main.py
exit
