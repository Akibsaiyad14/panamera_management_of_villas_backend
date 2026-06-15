@echo off
REM Celery Worker Startup Script for Windows Development
REM For Linux production deployment, use start_celery_worker.sh

echo ==========================================
echo Starting Celery Worker (Windows Dev)
echo ==========================================
echo Note: Using 'solo' pool for Windows compatibility
echo For production on Linux, use prefork pool
echo.

cd /d %~dp0panamera_backend

echo Activating virtual environment...
call ..\env\Scripts\activate.bat

echo Starting Celery Worker...
echo.

celery -A panamera_backend worker --loglevel=info --pool=solo

pause
