@echo off
setlocal
cd /d "%~dp0"

REM ============================================================
REM  RAG Graph QA launcher (Windows)
REM    start.bat              main service (port 8000)
REM    start.bat --mock       main + mock model service (no API key)
REM    start.bat --mock-only  mock model service only
REM    start.bat --help       show usage
REM ============================================================

if "%~1"=="--help" goto usage
if "%~1"=="-h" goto usage

if "%PORT%"=="" set "PORT=8000"
if "%MOCK_PORT%"=="" set "MOCK_PORT=9999"

REM ---- port availability check (fail early with a readable hint) ----
call :port_busy %PORT%
if errorlevel 1 (
  echo [x] Port %PORT% is already in use.
  echo     Most likely an earlier instance of this app is still running.
  echo.
  echo     Option 1: just open http://127.0.0.1:%PORT% - it may already be serving.
  echo     Option 2: stop the old process, then run this script again:
  echo                 netstat -ano ^| findstr :%PORT%
  echo                 taskkill /PID ^<pid^> /F
  echo     Option 3: use another port:
  echo                 set PORT=8010 ^&^& start.bat
  goto fail
)

REM ---- locate a usable Python ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo [x] Python not found. Install Python 3.10+ with "Add to PATH" checked.
  echo     https://www.python.org/downloads/
  goto fail
)

REM ---- dependency check ----
%PY% -c "import fastapi" >nul 2>nul
if errorlevel 1 (
  echo [*] First run: installing dependencies, this may take a few minutes...
  %PY% -m pip install -r backend\requirements.txt
  if errorlevel 1 goto fail
)

if "%~1"=="--mock-only" call :check_mock_port
if "%~1"=="--mock" call :check_mock_port

if "%~1"=="--mock-only" (
  echo [*] Mock model service: http://127.0.0.1:%MOCK_PORT%/v1
  %PY% mock_server.py --port %MOCK_PORT%
  goto done
)

if "%~1"=="--mock" (
  set "RAG_MOCK=1"
  echo [*] Mock model service: http://127.0.0.1:%MOCK_PORT%/v1
  start "RAG Mock Server" /min cmd /c "%PY% mock_server.py --port %MOCK_PORT%"
  timeout /t 2 >nul
  echo     Mock service runs in a separate minimized window; close it when done.
  echo     Main service points LLM / Embedding / Rerank to Mock automatically.
  echo.
)

echo [*] Main service: http://127.0.0.1:%PORT%
echo     Press Ctrl+C to stop
echo.
%PY% backend\run.py --port %PORT%
goto done

:check_mock_port
call :port_busy %MOCK_PORT%
if errorlevel 1 (
  echo [x] Mock port %MOCK_PORT% is already in use.
  echo     Another mock service may still be running. Stop it or set another port:
  echo                 set MOCK_PORT=9998 ^&^& start.bat --mock
  goto fail
)
exit /b 0

:port_busy
REM returns 0 when the port is free, 1 when something is listening on it
netstat -ano | findstr /C:":%~1 " | findstr /C:"LISTENING" >nul 2>nul
if errorlevel 1 exit /b 0
exit /b 1

:usage
echo Usage:
echo   start.bat              start main service on port 8000
echo   start.bat --mock       start main service + mock model service
echo   start.bat --mock-only  start mock model service only
echo   start.bat --help       show this help
echo.
echo Environment variables:
echo   PORT        main service port, default 8000
echo   MOCK_PORT   mock service port, default 9999
goto done

:fail
echo.
echo Startup failed. Fix the error above and try again.
pause

:done
endlocal
