"""
Celery and Redis Health Check Utilities
Use these functions to monitor Redis connection and Celery worker status
"""

from django.core.cache import cache
from celery import current_app
from datetime import datetime
import redis
from django.conf import settings


def check_redis_connection():
    """
    Check if Redis is accessible and responsive
    
    Returns:
        dict: Status information
    """
    try:
        # Try to connect directly to Redis
        redis_url = settings.CELERY_BROKER_URL
        redis_client = redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
        
        # Test SET and GET operations
        test_key = f"health_check_{datetime.now().timestamp()}"
        redis_client.set(test_key, "OK", ex=60)
        value = redis_client.get(test_key)
        redis_client.delete(test_key)
        
        # Get Redis info
        info = redis_client.info()
        
        return {
            'status': 'healthy',
            'connected': True,
            'redis_version': info.get('redis_version'),
            'used_memory': info.get('used_memory_human'),
            'connected_clients': info.get('connected_clients'),
            'uptime_days': info.get('uptime_in_days'),
            'message': 'Redis is running and accessible'
        }
        
    except redis.ConnectionError as e:
        return {
            'status': 'error',
            'connected': False,
            'error': 'Connection refused',
            'message': f'Cannot connect to Redis: {str(e)}',
            'recommendation': 'Check if Redis service is running: sudo systemctl status redis'
        }
    except redis.TimeoutError as e:
        return {
            'status': 'error',
            'connected': False,
            'error': 'Timeout',
            'message': f'Redis connection timeout: {str(e)}',
            'recommendation': 'Redis is slow to respond. Check server load and Redis performance.'
        }
    except Exception as e:
        return {
            'status': 'error',
            'connected': False,
            'error': type(e).__name__,
            'message': f'Unexpected error: {str(e)}'
        }


def check_celery_workers():
    """
    Check if Celery workers are running and responsive
    
    Returns:
        dict: Worker status information
    """
    try:
        # Get active workers using Celery inspect
        inspect = current_app.control.inspect(timeout=5)
        
        # Get active workers
        active_workers = inspect.active()
        registered_tasks = inspect.registered()
        stats = inspect.stats()
        
        if not active_workers:
            return {
                'status': 'warning',
                'workers_running': False,
                'worker_count': 0,
                'message': 'No active Celery workers detected',
                'recommendation': 'Start celery worker: supervisorctl start celery_worker'
            }
        
        # Count workers and tasks
        worker_names = list(active_workers.keys())
        total_workers = len(worker_names)
        
        return {
            'status': 'healthy',
            'workers_running': True,
            'worker_count': total_workers,
            'worker_names': worker_names,
            'registered_tasks': registered_tasks,
            'stats': stats,
            'message': f'{total_workers} Celery worker(s) running'
        }
        
    except Exception as e:
        return {
            'status': 'error',
            'workers_running': False,
            'error': type(e).__name__,
            'message': f'Failed to inspect Celery workers: {str(e)}',
            'recommendation': 'Check Celery broker connection and worker process'
        }


def check_celery_health():
    """
    Comprehensive health check for Celery + Redis
    
    Returns:
        dict: Complete health status
    """
    redis_status = check_redis_connection()
    celery_status = check_celery_workers()
    
    overall_healthy = (
        redis_status.get('status') == 'healthy' and 
        celery_status.get('status') == 'healthy'
    )
    
    return {
        'overall_status': 'healthy' if overall_healthy else 'degraded',
        'redis': redis_status,
        'celery': celery_status,
        'timestamp': datetime.now().isoformat(),
        'recommendations': _get_recommendations(redis_status, celery_status)
    }


def _get_recommendations(redis_status, celery_status):
    """Generate actionable recommendations based on health status"""
    recommendations = []
    
    if redis_status.get('status') != 'healthy':
        recommendations.append({
            'component': 'Redis',
            'issue': redis_status.get('message'),
            'action': redis_status.get('recommendation', 'Check Redis logs: journalctl -u redis -n 50')
        })
    
    if not celery_status.get('workers_running'):
        recommendations.append({
            'component': 'Celery Worker',
            'issue': celery_status.get('message'),
            'action': celery_status.get('recommendation', 'Check supervisor: supervisorctl status')
        })
    
    if not recommendations:
        recommendations.append({
            'component': 'All Systems',
            'status': 'Operational',
            'message': 'Redis and Celery are working correctly'
        })
    
    return recommendations


# Quick test function (can be called from management command or view)
def test_celery_task():
    """
    Send a test task to verify end-to-end Celery functionality
    
    Returns:
        dict: Test result
    """
    try:
        from api.tasks import check_leave_certificate_deadline
        
        # Try to schedule a test task (countdown 60 seconds, won't execute actual logic)
        result = check_leave_certificate_deadline.apply_async(
            args=[0],  # Invalid ID, task will return not_found
            countdown=60,
            task_id=f"health_check_test_{int(datetime.now().timestamp())}"
        )
        
        return {
            'status': 'success',
            'task_id': result.id,
            'message': 'Test task scheduled successfully',
            'note': 'Task will execute in 60 seconds (but will safely exit for test ID 0)'
        }
    except Exception as e:
        return {
            'status': 'error',
            'error': type(e).__name__,
            'message': f'Failed to schedule test task: {str(e)}'
        }
