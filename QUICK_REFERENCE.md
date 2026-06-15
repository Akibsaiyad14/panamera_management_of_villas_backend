# Quick Reference - Celery Commands

## Start Services

```bash
# Terminal 1: Celery Worker
cd ~/panamera_backend
./start_celery_worker.sh

# Terminal 2: Django Server  
cd ~/panamera_backend/panamera_backend
source ../env/bin/activate
python manage.py runserver 0.0.0.0:8000

# Terminal 3: Celery Beat (optional - for periodic tasks)
cd ~/panamera_backend
./start_celery_beat.sh
```

## Check Status

```bash
# Redis
redis-cli ping                    # Should return: PONG
sudo systemctl status redis-server

# Celery Worker
ps aux | grep "celery worker"

# Task Queue
redis-cli LLEN celery             # Number of pending tasks
```

## Test Celery

```bash
python manage.py shell
```
```python
from api.tasks import test_celery
result = test_celery.delay()
print(result.get(timeout=5))
# Expected: {'status': 'success', ...}
```

## Monitor Logs

```bash
# If using screen
screen -r celery-worker

# If using supervisor
tail -f /var/log/celery/worker.log
```

## Check Recent Sick Leaves

```sql
psql -d your_db_name -c "
SELECT id, \"leaveId\", \"leaveStatus\", \"rejectedBy\" 
FROM \"leaveApplication\" 
WHERE \"leaveType\" = 2 
ORDER BY id DESC LIMIT 5;"
```

## Restart Services

```bash
# Redis
sudo systemctl restart redis-server

# Celery (kill old, start new)
pkill -f "celery worker"
cd ~/panamera_backend
./start_celery_worker.sh

# Django
pkill -f "manage.py runserver"
cd ~/panamera_backend/panamera_backend
source ../env/bin/activate
python manage.py runserver 0.0.0.0:8000
```

## Change Deadline (Testing → Production)

```bash
nano ~/panamera_backend/panamera_backend/api/constants.py
# Line 68: Change from 1/60 to 12
# Save: Ctrl+X, Y, Enter

# Restart services after changing
```

## Using Screen (Keep Services Running)

```bash
# Start in screen
screen -S celery-worker
cd ~/panamera_backend && ./start_celery_worker.sh
# Detach: Ctrl+A, then D

screen -S django
cd ~/panamera_backend/panamera_backend
source ../env/bin/activate
python manage.py runserver 0.0.0.0:8000
# Detach: Ctrl+A, then D

# List screens
screen -ls

# Reattach
screen -r celery-worker
screen -r django
```

## Troubleshooting

```bash
# Check Redis logs
sudo journalctl -u redis-server -n 50

# Check if ports are in use
sudo netstat -tlnp | grep 6379    # Redis
sudo netstat -tlnp | grep 8000    # Django

# View Django logs for Celery scheduling
grep "CELERY" ~/panamera_backend/panamera_backend/*.log

# Clear Redis queue (caution!)
redis-cli FLUSHDB
```

---

**Full documentation:** See `CELERY_SETUP.md`
