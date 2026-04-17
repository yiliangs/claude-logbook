@echo off
REM Run the synthesizer with output logged to last_run.log for debugging.
REM Invoked by the Windows scheduled task "claude-activity-log synth" (see README).
REM Inherits ACTIVITY_LOG_* env vars from the logged-in user's environment.
cd /d "%~dp0"
python synthesizer.py > "%~dp0last_run.log" 2>&1
