#!/bin/bash
# Celery Beat Startup Script for Linux/Unix
# This script starts the Celery Beat scheduler for periodic tasks

cd "$(dirname "$0")/panamera_backend"

echo "=========================================="
echo "Starting Celery Beat Scheduler"
echo "=========================================="

# Ensure PID file directory exists (tmpfs /var/run is cleared on reboot)
if [ ! -d "/var/run/celery" ]; then
    sudo mkdir -p /var/run/celery
    sudo chown "$(whoami)":"$(whoami)" /var/run/celery
    echo "✓ Created /var/run/celery directory"
fi

# Activate virtual environment if it exists
if [ -d "../env/bin" ]; then
    source ../env/bin/activate
    echo "✓ Virtual environment activated"
fi

# Start Celery Beat scheduler
celery -A panamera_backend beat --loglevel=info \
    --scheduler django_celery_beat.schedulers:DatabaseScheduler \
    --pidfile /var/run/celery/beat.pid
