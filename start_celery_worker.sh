#!/bin/bash
# Celery Worker Startup Script for Linux/Unix
# This script starts the Celery worker for the panamera_backend application

cd "$(dirname "$0")/panamera_backend"

echo "=========================================="
echo "Starting Celery Worker"
echo "=========================================="

# Activate virtual environment if it exists
if [ -d "../env/bin" ]; then
    source ../env/bin/activate
    echo "✓ Virtual environment activated"
fi

# Start Celery worker
# Note: On Linux, we use 'prefork' pool (default) instead of 'solo'
# Concurrency defaults to number of CPU cores (optimal for most cases)
celery -A panamera_backend worker --loglevel=info
