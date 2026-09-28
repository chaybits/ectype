@echo off
rem ectype, portable build.
rem
rem Double-click this file. It starts the local web UI and opens it in your browser.
rem Closing this black window stops it, and so does Ctrl+C.
rem
rem Settings are kept in this folder, in user\settings.json, so the whole program is removed by
rem deleting the folder it lives in. To use the normal per-user location instead
rem (%APPDATA%\ectype\settings.json), put "rem " in front of the "set" line below.
setlocal
set "ECTYPE_CONFIG=%~dp0user\settings.json"

"%~dp0python\python.exe" -m ectype gui %*

rem Ctrl+C is a clean exit, so this only fires on a real failure: without it the message would
rem flash past as the window closed.
if errorlevel 1 (
  echo.
  echo ectype exited with an error. The message is above this line.
  pause
)
endlocal
