@echo off
REM Celery Beat Scheduler Startup Script for Windows Development
REM For Linux production deployment, use start_celery_beat.sh

echo ==========================================
echo Starting Celery Beat Scheduler
echo ==========================================
echo.

cd /d %~dp0panamera_backend

echo Activating virtual environment...
call ..\env\Scripts\activate.bat

echo Starting Celery Beat...
echo.

celery -A panamera_backend beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler

pause
