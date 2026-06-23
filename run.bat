@echo off
rem Launch Voice Dictation with no console window (pythonw). The app lives in the system tray.
cd /d D:\Dev\ai\projects\active\voice-dictation
start "" ".venv\Scripts\pythonw.exe" main.py
exit
