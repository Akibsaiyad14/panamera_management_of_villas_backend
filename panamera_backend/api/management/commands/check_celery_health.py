"""
Django management command to check Celery and Redis health
Usage: python manage.py check_celery_health
"""

from django.core.management.base import BaseCommand
from api.celery_health import check_celery_health, check_redis_connection, check_celery_workers, test_celery_task
import json


class Command(BaseCommand):
    help = 'Check Celery and Redis health status'

    def add_arguments(self, parser):
        parser.add_argument(
            '--test-task',
            action='store_true',
            help='Schedule a test task to verify end-to-end functionality',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Output results in JSON format',
        )

    def handle(self, *args, **options):
        self.stdout.write('=' * 70)
        self.stdout.write(self.style.SUCCESS('CELERY & REDIS HEALTH CHECK'))
        self.stdout.write('=' * 70)
        
        # Run health check
        health_status = check_celery_health()
        
        if options['json']:
            self.stdout.write(json.dumps(health_status, indent=2))
            return
        
        # Display results in human-readable format
        self._display_redis_status(health_status['redis'])
        self._display_celery_status(health_status['celery'])
        self._display_recommendations(health_status['recommendations'])
        
        # Overall status
        self.stdout.write('=' * 70)
        if health_status['overall_status'] == 'healthy':
            self.stdout.write(self.style.SUCCESS('✓ OVERALL STATUS: HEALTHY'))
        else:
            self.stdout.write(self.style.WARNING('⚠ OVERALL STATUS: DEGRADED'))
        self.stdout.write('=' * 70)
        
        # Test task if requested
        if options['test_task']:
            self.stdout.write('')
            self.stdout.write('Scheduling test task...')
            test_result = test_celery_task()
            if test_result['status'] == 'success':
                self.stdout.write(self.style.SUCCESS(f"✓ {test_result['message']}"))
                self.stdout.write(f"  Task ID: {test_result['task_id']}")
                self.stdout.write(f"  Note: {test_result['note']}")
            else:
                self.stdout.write(self.style.ERROR(f"✗ {test_result['message']}"))
    
    def _display_redis_status(self, redis_status):
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO('REDIS STATUS:'))
        self.stdout.write('-' * 70)
        
        if redis_status['status'] == 'healthy':
            self.stdout.write(self.style.SUCCESS(f"✓ Status: {redis_status['message']}"))
            self.stdout.write(f"  Redis Version: {redis_status.get('redis_version', 'N/A')}")
            self.stdout.write(f"  Memory Used: {redis_status.get('used_memory', 'N/A')}")
            self.stdout.write(f"  Connected Clients: {redis_status.get('connected_clients', 'N/A')}")
            self.stdout.write(f"  Uptime: {redis_status.get('uptime_days', 'N/A')} days")
        else:
            self.stdout.write(self.style.ERROR(f"✗ Status: {redis_status['message']}"))
            if 'recommendation' in redis_status:
                self.stdout.write(f"  Recommendation: {redis_status['recommendation']}")
    
    def _display_celery_status(self, celery_status):
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO('CELERY WORKER STATUS:'))
        self.stdout.write('-' * 70)
        
        if celery_status['status'] == 'healthy':
            self.stdout.write(self.style.SUCCESS(f"✓ Status: {celery_status['message']}"))
            self.stdout.write(f"  Active Workers: {celery_status['worker_count']}")
            for worker in celery_status.get('worker_names', []):
                self.stdout.write(f"    - {worker}")
        elif celery_status['status'] == 'warning':
            self.stdout.write(self.style.WARNING(f"⚠ Status: {celery_status['message']}"))
            if 'recommendation' in celery_status:
                self.stdout.write(f"  Recommendation: {celery_status['recommendation']}")
        else:
            self.stdout.write(self.style.ERROR(f"✗ Status: {celery_status['message']}"))
            if 'recommendation' in celery_status:
                self.stdout.write(f"  Recommendation: {celery_status['recommendation']}")
    
    def _display_recommendations(self, recommendations):
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO('RECOMMENDATIONS:'))
        self.stdout.write('-' * 70)
        
        for idx, rec in enumerate(recommendations, 1):
            if rec.get('status') == 'Operational':
                self.stdout.write(self.style.SUCCESS(f"✓ {rec['message']}"))
            else:
                self.stdout.write(f"{idx}. Component: {rec['component']}")
                self.stdout.write(f"   Issue: {rec['issue']}")
                self.stdout.write(self.style.WARNING(f"   Action: {rec['action']}"))
                self.stdout.write('')
