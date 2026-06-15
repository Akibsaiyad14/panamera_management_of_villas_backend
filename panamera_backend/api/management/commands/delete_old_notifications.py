# your_app/management/commands/delete_old_notifications.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import connection, transaction
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Permanently deletes notifications older than N days. Does NOT touch other tables.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=14,
            help='Number of days to keep notifications (default: 14).',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Batch size for deletions to avoid table locks (default: 1000).',
        )

    def handle(self, *args, **options):
        days = options['days']
        batch_size = options['batch_size']
        cutoff_date = timezone.now() - timezone.timedelta(days=days)
        
        deleted_notifications = 0

        with transaction.atomic():  # Wrap in transaction for rollback on error
            # Delete old notifications in batches using CTE (PostgreSQL-compatible)
            while True:
                with connection.cursor() as cursor:
                    query = """
                        WITH rows_to_delete AS (
                            SELECT "notificationId" 
                            FROM "notifications" 
                            WHERE "createdAt" < %s
                            LIMIT %s
                        )
                        DELETE FROM "notifications" 
                        WHERE "notificationId" IN (SELECT "notificationId" FROM rows_to_delete)
                    """
                    cursor.execute(query, [cutoff_date, batch_size])
                    batch_deleted = cursor.rowcount
                    deleted_notifications += batch_deleted
                    if batch_deleted < batch_size:
                        break  # No more to delete

        self.stdout.write(
            self.style.SUCCESS(f'Successfully deleted {deleted_notifications} notifications older than {days} days.')
        )

        logger.info(f'Deletion complete: {deleted_notifications} notifications')
