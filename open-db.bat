@echo off
REM Starts the AgentCare Postgres container (and Adminer, a web DB browser)
REM and opens Adminer in your default browser, pre-filled with the login
REM fields (you still need to type the password: agentcare).
REM
REM Personal dev convenience only - not part of the submitted app.

echo Starting Postgres (db service)...
docker compose up -d db
if errorlevel 1 (
    echo Failed to start the db service. Is Docker Desktop running?
    pause
    exit /b 1
)

echo Waiting for Postgres to be healthy...
:waitloop
set STATUS=
for /f %%i in ('docker inspect --format="{{.State.Health.Status}}" agent_care-db-1 2^>nul') do set STATUS=%%i
if "%STATUS%"=="healthy" goto ready
timeout /t 2 >nul
goto waitloop

:ready
echo Postgres is healthy.

docker ps -a --filter "name=agentcare-adminer" --format "{{.Names}}" | findstr /i "agentcare-adminer" >nul
if %errorlevel%==0 (
    echo Starting existing Adminer container...
    docker start agentcare-adminer >nul
) else (
    echo Creating Adminer container...
    docker run -d --name agentcare-adminer --network agent_care_default -p 8081:8080 adminer >nul
)

timeout /t 2 >nul
echo Opening Adminer in your browser...
start "" "http://localhost:8081/?pgsql=db&username=agentcare&db=agentcare"

echo Done. Password is: agentcare
pause
