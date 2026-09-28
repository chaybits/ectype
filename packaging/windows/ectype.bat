@echo off
rem ectype, portable build. This is the command line; ectype-gui.bat is the clickable one.
rem
rem Open a Command Prompt in this folder and run, for example:
rem
rem     ectype agents                 which agents are on this machine, and where
rem     ectype list                   the newest sessions across all of them
rem     ectype show <id>              read one
rem     ectype export <id> -o out.txt write one out
rem
rem Double-clicking this file prints the full command list instead.
setlocal
set "ECTYPE_CONFIG=%~dp0user\settings.json"

if "%~1"=="" (
  "%~dp0python\python.exe" -m ectype --help
  echo.
  pause
) else (
  "%~dp0python\python.exe" -m ectype %*
)
endlocal
