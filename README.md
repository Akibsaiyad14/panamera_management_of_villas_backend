# Panamera Backend - Project Overview and Flow

This document describes the architecture, runtime flow, and developer setup for the Panamera backend (Django + Celery). It summarizes how requests move through the system, where background tasks are scheduled and executed, and how notifications and persistence are handled.

**Project Structure (key files)**
- `panamera_backend/manage.py` - Django CLI entry point.
- [panamera_backend/panamera_backend/settings.py](panamera_backend/panamera_backend/settings.py#L1) - Application and Celery configuration.
- [panamera_backend/panamera_backend/celery.py](panamera_backend/panamera_backend/celery.py#L1) - Celery app, autodiscovery, and example beat schedule.
- [panamera_backend/panamera_backend/__init__.py](panamera_backend/panamera_backend/__init__.py#L1) - Imports Celery app so it is available on Django startup.
- [panamera_backend/panamera_backend/urls.py](panamera_backend/panamera_backend/urls.py#L1) - URL routing, includes `api.urls`.
- `panamera_backend/api/` - Main application: views, tasks, utils, models.
  - [panamera_backend/api/tasks.py](panamera_backend/api/tasks.py#L1) - Core Celery tasks (e.g., `check_leave_certificate_deadline`).
  - [panamera_backend/api/views/leave_applications.py](panamera_backend/api/views/leave_applications.py#L990) - Leave application flow and Celery scheduling logic.
  - [panamera_backend/api/utils.py](panamera_backend/api/utils.py#L1) - DB helpers (`execute_query`), notification storage and push helpers (`_send_notification`).

Architecture summary
- Web/API: Django + Django REST Framework handling HTTP requests.
- Database: PostgreSQL (configured via environment variables in settings).
- Background processing: Celery configured in [panamera_backend/panamera_backend/celery.py](panamera_backend/panamera_backend/celery.py#L1) and settings in [panamera_backend/panamera_backend/settings.py](panamera_backend/panamera_backend/settings.py#L324). Broker/result backend expected to be Redis (configurable via `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`).
- Scheduled jobs: Celery Beat using `django_celery_beat` (settings: `CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'`).
- Push notifications: Stored in DB via `_send_notification` and sent to Firebase in a background thread (see `api/utils.py`).

Primary runtime flow (example: Leave application & auto-reject flow)
1. Client submits a leave application to the API (endpoint in `api/views/leave_applications.py`). The view inserts the leave record into DB.
2. If the leave is a sick leave, the view computes a certificate upload deadline and schedules a Celery task:
   - The view calls `check_leave_certificate_deadline.apply_async(args=[leave_id], countdown=<seconds>, task_id=...)` (see [leave_applications.py](panamera_backend/api/views/leave_applications.py#L1034)).
3. Celery worker picks up the scheduled task at the deadline and runs `api.tasks.check_leave_certificate_deadline`:
   - The task queries the DB using `execute_query`.
   - If certificate is missing and leave still pending, it updates the `leaveApplication` row to mark overdue + rejected.
   - It logs activity via `log_activity_raw` and sends notifications.
4. Notifications are persisted to the `notifications` table by `_send_notification` (in `api/utils.py`) and push delivery to FCM is attempted in a separate thread. Missing FCM tokens are handled and cleaned up.

Other notable flows
- Task creation/assignment, emergency requests, and team assignment flows all use similar patterns: API view writes DB, optionally schedule tasks or send notifications using `_send_notification`.
- Periodic maintenance tasks (e.g., `cleanup_expired_leave_tasks`) are defined in `api/tasks.py` and can be scheduled via Celery Beat.

Developer setup (quickstart)
1. Create a Python virtual environment and activate it.

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Unix
source .venv/bin/activate
```

2. Install dependencies (project lists requirements in `requirment.txt` or workspace docs). Example:

```bash
pip install -r requirment.txt
```

3. Create an `.env` at project root with environment variables used in settings, at minimum:
- `SECRET_KEY`
- `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_PASSWORD`, `DATABASE_HOST`, `DATABASE_PORT`
- `CELERY_BROKER_URL` (e.g., `redis://localhost:6379/0`)
- `CELERY_RESULT_BACKEND` (e.g., `redis://localhost:6379/0`)
- Email settings: `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (if emails are used)

4. Run migrations and create a superuser:

```bash
python panamera_backend/manage.py migrate
python panamera_backend/manage.py createsuperuser
```

5. Start Django dev server:

```bash
python panamera_backend/manage.py runserver
```

6. Start Celery worker and beat (examples provided in project root):

Windows (batch scripts exist):

```powershell
start_celery_worker.bat
start_celery_beat.bat
```

Unix shell versions also exist:

```bash
./start_celery_worker.sh
./start_celery_beat.sh
```

Important runtime notes
- Celery-related environment variables and Redis availability are required for scheduled certificate checks to work. If Celery/Redis is not available the API will still create leaves but will log a warning and will not schedule the automated check.
- `execute_query` in `api/utils.py` uses raw SQL and must properly handle transactions. Rollbacks are attempted on error.
- Notifications are stored in DB and pushed via Firebase in threads; ensure Firebase credentials are configured when using push.

Where to look for more details
- API endpoints and per-feature flows: `panamera_backend/api/views/` (many views split into files, e.g., `leave_applications.py`, `task_manager.py`).
- Celery tasks: `panamera_backend/api/tasks.py`.
- DB patches and SQL helpers: top-level SQL files and `helper_script/`.
- Celery and deployment notes: `CELERY_SETUP.md`, `CELERY_ERROR_HANDLING.md`, `CRON_SETUP.md`.

Next steps / Suggestions
- Add a short `CONTRIBUTING.md` describing how to run unit tests and code style checks.
- Document environment variable keys and example `.env.example` (I can create this if you want).

---
Generated by an automated repository scan. If you want more detail on any specific flow (auth, notifications, task routing), tell me which area to expand.
