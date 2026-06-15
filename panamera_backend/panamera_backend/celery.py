"""
Celery configuration for Panamera Backend
This module sets up Celery for handling asynchronous tasks and scheduled jobs.
"""

import os
from celery import Celery
from django.conf import settings

# Set default Django settings module for Celery
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'panamera_backend.settings')

# Create Celery app instance
app = Celery('panamera_backend')

# Load configuration from Django settings using CELERY_ prefix
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed Django apps
app.autodiscover_tasks()

# Optional: Configure periodic tasks (beat schedule) here if needed
# This is for recurring tasks, not dynamic one-time tasks
app.conf.beat_schedule = {
    # Example: Clean up expired tasks every day
    'cleanup-expired-tasks': {
        'task': 'api.tasks.cleanup_expired_leave_tasks',
        'schedule': 86400.0,  # Run every 24 hours
    },
}

@app.task(bind=True)
def debug_task(self):
    """Debug task to test Celery is working"""
    print(f'Request: {self.request!r}')
