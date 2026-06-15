import os
import json
import traceback
from datetime import datetime, timedelta
import pytz
from django.conf import settings
from django.db import IntegrityError
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from ..utils import error_response, success_response, execute_query, log_activity_raw, send_mail_with_template_async, send_plain_email_async, _send_notification
from ..constants import *


class EmergencyRequestView(APIView):
    permission_classes = [IsAuthenticated]
    allowed_categories = {"Garden", "Pool", "Other"}
    allowed_sort_fields = {
        "createdAt",
        "requestId",
        "customerId",
        "teamLeaderId",
        "supervisorId",
        "assignedTime",
        "closedTime",
        "responseTime",
        "amount",
        "requestStatus",
    }

    def _normalize_image_paths(self, images):
        if isinstance(images, str):
            try:
                parsed = json.loads(images)
                return parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                return []
        if isinstance(images, list):
            return images
        return []

    def _serialize_request_row(self, request, row):
        if not isinstance(row, dict):
            return row

        row['images'] = self._build_image_urls(request, self._normalize_image_paths(row.get('images')))
        return row

    def _get_user_details(self, user_id):
        if not user_id:
            return None

        user_query = '''
            SELECT id, "userName", "fullName"
            FROM public."user"
            WHERE id = %s AND COALESCE("isDeleted", 0) = 0
            LIMIT 1
        '''
        user_result = execute_query(user_query, [user_id], fetch='one')
        if isinstance(user_result, list) and user_result:
            user_result = user_result[0]
        return user_result if isinstance(user_result, dict) else None

    def _save_request_images(self, request_id, image_files):
        saved_paths = []
        upload_dir = os.path.join(settings.MEDIA_ROOT, "emergency_requests", str(request_id), "images")
        os.makedirs(upload_dir, exist_ok=True)

        for image_file in image_files:
            filename = image_file.name
            if filename.lower().endswith('.jfif'):
                filename = filename[:-5] + '.png'

            file_path = os.path.join(upload_dir, filename)
            with open(file_path, 'wb+') as destination:
                for chunk in image_file.chunks():
                    destination.write(chunk)

            relative_path = os.path.relpath(file_path, settings.MEDIA_ROOT).replace('\\', '/')
            saved_paths.append(relative_path)

        return saved_paths

    def _build_image_urls(self, request, image_paths):
        return [request.build_absolute_uri(settings.MEDIA_URL + path) for path in image_paths]

    def _get_status_name(self, status_code):
        """Convert numeric request status code to human-readable status name."""
        status_map = {
            OPEN: 'Open',
            CLOSED: 'Closed',
            ON_HOLD: 'On Hold',
            IN_PROGRESS: 'In Progress',
            AWAITING_GATE_PASS: 'Awaiting Gate Pass',
            QUOTATION_STAGE: 'Quotation Stage',
            JOB_APPROVED: 'Job Approved',
            CANCELLED: 'Cancelled',
        }
        return status_map.get(status_code, f'Unknown ({status_code})')

    def _get_customer_villa_id(self, customer_id):
        villa_query = '''
            SELECT id
            FROM public."villaDetails"
            WHERE "customerId" = %s AND COALESCE("isDeleted", 0) = 0
            ORDER BY id ASC
            LIMIT 1
        '''
        villa_result = execute_query(villa_query, [customer_id], fetch='one')
        if isinstance(villa_result, list) and villa_result:
            villa_result = villa_result[0]

        if isinstance(villa_result, dict):
            return villa_result.get('id')
        return None

    def _get_customer_notification_target(self, customer_id):
        query = '''
            SELECT id, "customerId", "fcmToken", "customerName"
            FROM public.customer
            WHERE id = %s AND COALESCE("isDeleted", 0) = 0
            LIMIT 1
        '''
        result = execute_query(query, [customer_id], fetch='one')
        if isinstance(result, list) and result:
            result = result[0]
        return result if isinstance(result, dict) else None

    def _get_users_by_group_numbers(self, group_numbers):
        if not group_numbers:
            return []

        placeholders = ", ".join(["%s"] * len(group_numbers))
        query = f'''
            SELECT u.id, u."fullName", u."fcmToken"
            FROM public."user" u
            INNER JOIN public."userrole" ur ON u."roleId" = ur."roleId"
            WHERE ur."groupNumber" IN ({placeholders})
              AND COALESCE(u."isDeleted", '0') = '0'
        '''
        result = execute_query(query, list(group_numbers), fetch='all', many=True)
        return result if isinstance(result, list) else []

    def _get_estimator_users(self):
        query = '''
            SELECT u.id, u."fullName", u."fcmToken"
            FROM public."user" u
            INNER JOIN public."userrole" ur ON u."roleId" = ur."roleId"
            WHERE ur."groupNumber" = %s
              AND COALESCE(u."isDeleted", '0') = '0'
        '''
        result = execute_query(query, [ESTIMATOR_GROUP_NUMBER], fetch='all', many=True)
        return result if isinstance(result, list) else []

    def _get_user_notification_target(self, user_id):
        if not user_id:
            return None
        query = '''
            SELECT id, "fullName", "fcmToken"
            FROM public."user"
            WHERE id = %s AND COALESCE("isDeleted", '0') = '0'
            LIMIT 1
        '''
        result = execute_query(query, [user_id], fetch='one')
        if isinstance(result, list) and result:
            result = result[0]
        return result if isinstance(result, dict) else None

    def _get_office_admin_emails(self):
        admin_email_query = '''
            SELECT "officeAdminEmail"
            FROM "AdminSettings"
            WHERE COALESCE("isDeleted", 0) = 0
            LIMIT 1
        '''
        admin_email_result = execute_query(admin_email_query, [], fetch='one')
        if isinstance(admin_email_result, list) and admin_email_result:
            admin_email_result = admin_email_result[0]
        if isinstance(admin_email_result, dict):
            raw_email = (admin_email_result.get('officeAdminEmail') or '').strip()
            if not raw_email:
                return []
            normalized = raw_email.replace(';', ',')
            return [email.strip() for email in normalized.split(',') if email.strip()]
        return []

    def _send_payment_status_emails(self, updated, payment_status, payment_id, transaction_id):
        customer_id = updated.get('customerId') if isinstance(updated, dict) else None
        if not customer_id:
            return

        customer_query = '''
            SELECT email, "customerName"
            FROM public.customer
            WHERE id = %s AND COALESCE("isDeleted", 0) = 0
            LIMIT 1
        '''
        customer_result = execute_query(customer_query, [customer_id], fetch='one')
        if isinstance(customer_result, list) and customer_result:
            customer_result = customer_result[0]

        customer_email = customer_result.get('email') if isinstance(customer_result, dict) else None
        customer_name = customer_result.get('customerName') if isinstance(customer_result, dict) else None

        payment_status_text = 'Success' if payment_status == PAYMENT_SUCCESS else 'Failed'
        template_name = 'Emergency Payment Success' if payment_status == PAYMENT_SUCCESS else 'Emergency Payment Failed'
        email_context = {
            'Customer Name': customer_name or 'Customer',
            'Request Id': updated.get('requestId'),
            'Transaction Id': transaction_id if transaction_id else 'N/A',
            'Payment Id': payment_id if payment_id else 'N/A',
            'Amount': updated.get('amount'),
            'Payment Status': payment_status_text,
            'Issue Name': updated.get('issueName') or 'N/A',
        }

        if customer_email:
            try:
                template_result = execute_query(
                    '''
                    SELECT subject, body
                    FROM "mailTemplates"
                    WHERE "templateName" = %s AND COALESCE("isDeleted", 0) = 0
                    LIMIT 1
                    ''',
                    [template_name],
                    fetch='one'
                )
                if template_result:
                    send_mail_with_template_async(template_name=template_name, recipient_email=customer_email, context=email_context)
                else:
                    subject = f"Emergency request payment {payment_status_text.lower()}"
                    body = (
                        f"Hello {email_context['Customer Name']},\n\n"
                        f"Your emergency request payment is {payment_status_text.lower()}.\n"
                        f"Request Id: {email_context['Request Id']}\n"
                        f"Transaction Id: {email_context['Transaction Id']}\n"
                        f"Payment Id: {email_context['Payment Id']}\n"
                        f"Amount: {email_context['Amount']}\n"
                        f"Issue Name: {email_context['Issue Name']}\n"
                    )
                    send_plain_email_async(subject, body, customer_email, settings.DEFAULT_FROM_EMAIL)
            except Exception:
                pass

        admin_emails = self._get_office_admin_emails()
        if admin_emails:
            admin_template_name = 'Emergency Payment Admin Notification'
            admin_context = {
                'Request Id': updated.get('requestId'),
                'Amount': updated.get('amount'),
                'Transaction Id': transaction_id or 'N/A',
                'Payment Status': payment_status_text,
                'Customer Name': customer_name or 'Customer',
                'Issue Name': updated.get('issueName') or 'N/A',
            }
            try:
                admin_template_result = execute_query(
                    '''
                    SELECT subject, body
                    FROM "mailTemplates"
                    WHERE "templateName" = %s AND COALESCE("isDeleted", 0) = 0
                    LIMIT 1
                    ''',
                    [admin_template_name],
                    fetch='one'
                )
                for admin_email in admin_emails:
                    if admin_template_result:
                        send_mail_with_template_async(
                            template_name=admin_template_name,
                            recipient_email=admin_email,
                            context=admin_context,
                        )
                    else:
                        subject = f"Emergency payment {payment_status_text.lower()}: {admin_context['Request Id']}"
                        body = (
                            f"Hello Office Admin,\n\n"
                            f"Emergency request payment is {payment_status_text.lower()}.\n"
                            f"Request Id: {admin_context['Request Id']}\n"
                            f"Transaction Id: {admin_context['Transaction Id']}\n"
                            f"Amount: {admin_context['Amount']}\n"
                            f"Issue Name: {admin_context['Issue Name']}\n"
                        )
                        send_plain_email_async(subject, body, admin_email, settings.DEFAULT_FROM_EMAIL)
            except Exception:
                pass

    def _notify_customer(self, customer_id, title, body, notification_type, data_payload):
        customer = self._get_customer_notification_target(customer_id)
        if not customer:
            return
        try:
            _send_notification(
                recipient_customer_id=customer.get('customerId'),
                title=title,
                body=body,
                notification_type=notification_type,
                data_payload=data_payload,
                fcm_token=customer.get('fcmToken'),
                delay_seconds=60,
            )
        except Exception:
            pass

    def _notify_users(self, users, title, body, notification_type, data_payload):
        for user in users:
            try:
                _send_notification(
                    recipient_user_id=user.get('id'),
                    title=title,
                    body=body,
                    notification_type=notification_type,
                    data_payload=data_payload,
                    fcm_token=user.get('fcmToken'),
                    delay_seconds=60,
                )
            except Exception:
                pass

    def _cleanup_saved_images(self, request_id):
        upload_dir = os.path.join(settings.MEDIA_ROOT, "emergency_requests", str(request_id))
        if os.path.exists(upload_dir):
            for root, dirs, files in os.walk(upload_dir, topdown=False):
                for file_name in files:
                    try:
                        os.remove(os.path.join(root, file_name))
                    except OSError:
                        pass
                for directory in dirs:
                    try:
                        os.rmdir(os.path.join(root, directory))
                    except OSError:
                        pass
            try:
                os.rmdir(upload_dir)
            except OSError:
                pass

    def _generate_request_id(self):
        dubai_tz = pytz.timezone('Asia/Dubai')
        today_prefix = datetime.now(dubai_tz).strftime('ESR%y%m')

        seq_query = '''
            SELECT COALESCE(MAX(CAST(RIGHT("requestId", 4) AS INTEGER)), 0) AS last_sequence
            FROM public."emergencyRequest"
            WHERE "requestId" LIKE %s
        '''
        result = execute_query(seq_query, [f"{today_prefix}%"], fetch='one')
        if isinstance(result, list) and result:
            result = result[0]

        last_sequence = 0
        if isinstance(result, dict):
            last_sequence = int(result.get('last_sequence') or 0)

        return f"{today_prefix}{last_sequence + 1:04d}"

    @transaction.atomic
    def post(self, request):
        try:
            data = getattr(request, 'data', None) or request.POST
            customer_id = data.get('customerId')
            description = data.get('description', '').strip()
            category = data.get('category', '').strip()
            service_type = data.get('serviceType', 0)
            villa_id = data.get('villaId')
            amount = data.get('amount', 0)
            image_files = request.FILES.getlist('images') if hasattr(request, 'FILES') else []

            if not customer_id:
                return error_response(message='customerId is required.', status_code=status.HTTP_400_BAD_REQUEST)

            if not description:
                return error_response(message='description is required.', status_code=status.HTTP_400_BAD_REQUEST)

            if not category:
                return error_response(message='category is required.', status_code=status.HTTP_400_BAD_REQUEST)

            if category not in self.allowed_categories:
                return error_response(message='category must be either Garden, Pool, or Other.', status_code=status.HTTP_400_BAD_REQUEST)

            # if not image_files:
            #     return error_response(message='At least one image is required.', status_code=status.HTTP_400_BAD_REQUEST)

            try:
                customer_id = int(customer_id)
            except (TypeError, ValueError):
                return error_response(message='customerId must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)

            try:
                service_type = int(service_type)
            except (TypeError, ValueError):
                return error_response(message='serviceType must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)

            if service_type not in (0, 1):
                return error_response(message='serviceType must be 0 for Regular or 1 for Off Hours.', status_code=status.HTTP_400_BAD_REQUEST)

            customer_query = '''
                SELECT id, "customerId", "customerName"
                FROM public.customer
                WHERE id = %s AND COALESCE("isDeleted", 0) = 0
                LIMIT 1
            '''
            customer_result = execute_query(customer_query, [customer_id], fetch='one')
            if isinstance(customer_result, list) and customer_result:
                customer_result = customer_result[0]

            if not customer_result:
                return error_response(message='Customer not found.', status_code=status.HTTP_404_NOT_FOUND)

            inserted = None
            request_id = None
            image_paths = []

            insert_query = '''
                INSERT INTO public."emergencyRequest"
                ("requestId", "villaId", "customerId", category, description, images, "serviceType", "requestStatus", "createdAt", "updatedAt", "isDeleted", amount)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, NOW(), NOW(), 0, %s)
                RETURNING id, "requestId", "villaId", "customerId", category, description, images, "serviceType", "requestStatus", "teamLeaderId", "supervisorId", "assignedTime", "closedTime", "paymentUpdatedAt", "responseTime", "resolutionTime", "createdAt", "updatedAt"
            '''

            for _ in range(3):
                request_id = self._generate_request_id()
                image_paths = self._save_request_images(request_id, image_files)
                insert_params = [
                    request_id,
                    villa_id,
                    customer_id,
                    category,
                    description,
                    json.dumps(image_paths),
                    service_type,
                    0,
                    amount,
                ]

                try:
                    inserted = execute_query(insert_query, insert_params, fetch='one')
                    if isinstance(inserted, list) and inserted:
                        inserted = inserted[0]
                    if inserted:
                        break
                except IntegrityError:
                    self._cleanup_saved_images(request_id)
                    inserted = None
                    continue

            if not inserted:
                if request_id:
                    self._cleanup_saved_images(request_id)
                return error_response(message='Failed to create emergency request.', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            inserted = self._serialize_request_row(request, inserted)
            inserted['villaId'] = self._get_customer_villa_id(customer_id)

            villa_name = None
            if villa_id:
                villa_name_result = execute_query(
                    'SELECT "villaName" FROM public."villaDetails" WHERE id = %s AND COALESCE("isDeleted", 0) = 0 LIMIT 1',
                    [villa_id],
                    fetch='one'
                )
                if isinstance(villa_name_result, list) and villa_name_result:
                    villa_name_result = villa_name_result[0]
                if isinstance(villa_name_result, dict):
                    villa_name = villa_name_result.get('villaName')
            customer_name = None
            if isinstance(customer_result, dict):
                customer_name = customer_result.get('customerName')

            log_activity_raw(
                request=request,
                category='EmergencyRequest',
                action='Add',
                performer=getattr(request, 'user', None),
                details={
                    'requestId': request_id,
                    'customerId': customer_id,
                    'category': category,
                    'imageCount': len(image_paths),
                    'amount': amount,
                }
            )

            try:
                service_tag = 'Normal' if service_type == 0 else 'Off Hours'
                supervisors = self._get_users_by_group_numbers([ROLE_GROUP_SUPERVISOR])
                villa_label = villa_name or 'N/A'
                customer_label = customer_name or 'N/A'
                self._notify_users(
                    users=supervisors,
                    title=f"Emergency Request #{request_id}",
                    body=f"Emergency Request #{request_id} [{service_tag}] has been submitted. Villa: {villa_label}. Customer: {customer_label}.",
                    notification_type='EMERGENCY_REQUEST_SUBMITTED_SUPERVISOR',
                    data_payload={'requestId': str(request_id)},
                )
            except Exception:
                pass

            return success_response(
                data=inserted,
                message='Emergency request created successfully.',
                status_code=status.HTTP_201_CREATED,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f'Error creating emergency request: {str(e)}', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def get(self, request, request_id=None):
        try:
            if request_id:
                query = '''
                    SELECT id, "requestId", "villaId", "customerId", category, description, images,
                           "serviceType", "requestStatus", "teamLeaderId", "supervisorId", "assignedTime", "closedTime", "paymentUpdatedAt", "responseTime", "resolutionTime",
                           "paymentStatus", "paymentId", "transactionId", "hashId", "rawUrl", amount, "createdAt", "updatedAt", "issueName"
                    FROM public."emergencyRequest"
                    WHERE "requestId" = %s AND COALESCE("isDeleted", 0) = 0
                    LIMIT 1
                '''
                result = execute_query(query, [request_id], fetch='one')
                if isinstance(result, list) and result:
                    result = result[0]
                if not result:
                    return error_response(message='Emergency request not found.', status_code=status.HTTP_404_NOT_FOUND)

                result = self._serialize_request_row(request, result)
                if result.get('teamLeaderId'):
                    team_leader_user = self._get_user_details(result.get('teamLeaderId'))
                    if team_leader_user:
                        result['teamLeaderName'] = team_leader_user.get('fullName') or team_leader_user.get('userName')
                        result['teamLeaderUserName'] = team_leader_user.get('userName')
                if result.get('supervisorId'):
                    supervisor_user = self._get_user_details(result.get('supervisorId'))
                    if supervisor_user:
                        result['supervisorName'] = supervisor_user.get('fullName') or supervisor_user.get('userName')
                        result['supervisorUserName'] = supervisor_user.get('userName')
                return success_response(data=result, message='Emergency request fetched successfully.')

            search = request.query_params.get("search", "").strip()
            request_id_filter = request.query_params.get("requestId", "").strip()
            customer_id_filter = request.query_params.get("customerId", "").strip()
            villa_id_filter = request.query_params.get("villaId", "").strip()
            category_filter = request.query_params.get("category", "").strip()
            service_type_filter = request.query_params.get("serviceType", "").strip()
            team_leader_filter = request.query_params.get("teamLeaderId", "").strip()
            if not team_leader_filter:
                team_leader_filter = request.query_params.get("assignedTo", "").strip()
            supervisor_id_filter = request.query_params.get("supervisorId", "").strip()
            payment_status_filter = request.query_params.get("paymentStatus", "").strip()
            start_date = request.query_params.get("startDate", "").strip()
            end_date = request.query_params.get("endDate", "").strip()
            sort_param = request.query_params.get("sort", "createdAt").strip() or "createdAt"
            order_param = request.query_params.get("order", "desc").strip().lower() or "desc"
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            request_status = request.query_params.get("requestStatus", "").strip()
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            assigned_filter = request.query_params.get("assigned", "").strip().lower() == "true"
            pending_unassigned_count_only = request.query_params.get("pendingEsrCount", "false").strip().lower() == "true"

            if pending_unassigned_count_only:
                pending_where_conditions = [
                    'COALESCE(er."isDeleted", 0) = 0',
                    'er."paymentStatus" = %s',
                    'er."supervisorId" IS NULL',
                ]
                pending_params = [PAYMENT_SUCCESS]

                if start_date and end_date:
                    pending_where_conditions.append('er."createdAt"::date BETWEEN %s AND %s')
                    pending_params.extend([start_date, end_date])
                elif start_date:
                    pending_where_conditions.append('er."createdAt"::date >= %s')
                    pending_params.append(start_date)
                elif end_date:
                    pending_where_conditions.append('er."createdAt"::date <= %s')
                    pending_params.append(end_date)

                pending_where_clause = " WHERE " + " AND ".join(pending_where_conditions)
                pending_count_query = f'''
                    SELECT COUNT(*) AS "pendingUnassignedCount"
                    FROM public."emergencyRequest" er
                    {pending_where_clause}
                '''
                pending_count_result = execute_query(pending_count_query, pending_params, fetch='one')
                if isinstance(pending_count_result, list) and pending_count_result:
                    pending_count_result = pending_count_result[0]
                pending_unassigned_count = int(pending_count_result.get('pendingUnassignedCount') or 0) if isinstance(pending_count_result, dict) else 0

                return success_response(
                    data=pending_unassigned_count,
                    message='Pending unassigned emergency request count fetched successfully.',
                    status_code=status.HTTP_200_OK,
                )

            where_conditions = ['COALESCE(er."isDeleted", 0) = 0']
            params = []

            if search:
                where_conditions.append('''
                    (
                        er."requestId" ILIKE %s OR
                        er.description ILIKE %s OR
                        c."customerName" ILIKE %s OR
                        CAST(er."customerId" AS TEXT) ILIKE %s OR
                        tl."userName" ILIKE %s OR
                        tl."fullName" ILIKE %s OR
                        su."userName" ILIKE %s OR
                        su."fullName" ILIKE %s OR
                        CAST(er."teamLeaderId" AS TEXT) ILIKE %s OR
                        CAST(er."supervisorId" AS TEXT) ILIKE %s
                    )
                ''')
                search_param = f"%{search}%"
                params.extend([search_param] * 10)

            if request_id_filter:
                where_conditions.append('er."requestId" ILIKE %s')
                params.append(f"%{request_id_filter}%")

            if request_status:
                where_conditions.append('er."requestStatus" = %s')
                params.append(request_status)

            if customer_id_filter:
                where_conditions.append('er."customerId" = %s')
                params.append(customer_id_filter)

            if villa_id_filter:
                where_conditions.append('er."villaId" = %s')
                params.append(villa_id_filter)

            if category_filter:
                where_conditions.append('er.category = %s')
                params.append(category_filter)

            if service_type_filter:
                where_conditions.append('er."serviceType" = %s')
                params.append(service_type_filter)

            if team_leader_filter:
                where_conditions.append('er."teamLeaderId" = %s')
                params.append(team_leader_filter)

            if supervisor_id_filter:
                where_conditions.append('er."supervisorId" = %s')
                params.append(supervisor_id_filter)

            if payment_status_filter:
                where_conditions.append('er."paymentStatus" = %s')
                params.append(payment_status_filter)

            if assigned_filter:
                where_conditions.append('er."supervisorId" IS NOT NULL')

            if start_date and end_date:
                where_conditions.append('er."createdAt"::date BETWEEN %s AND %s')
                params.extend([start_date, end_date])
            elif start_date:
                where_conditions.append('er."createdAt"::date >= %s')
                params.append(start_date)
            elif end_date:
                where_conditions.append('er."createdAt"::date <= %s')
                params.append(end_date)

            sort_field = sort_param if sort_param in self.allowed_sort_fields else "createdAt"
            order_dir = "asc" if order_param == "asc" else "desc"

            where_clause = " WHERE " + " AND ".join(where_conditions)

            count_query = f'''
                SELECT COUNT(*) AS total
                FROM public."emergencyRequest" er
                LEFT JOIN public."customer" c ON er."customerId" = c."id"
                LEFT JOIN public."villaDetails" v ON er."villaId" = v."id"
                LEFT JOIN public."user" tl ON er."teamLeaderId" = tl."id"
                LEFT JOIN public."user" su ON er."supervisorId" = su."id"
                {where_clause}
            '''
            count_result = execute_query(count_query, params, fetch='one')
            if isinstance(count_result, list) and count_result:
                count_result = count_result[0]
            total_count = int(count_result.get('total') or 0) if isinstance(count_result, dict) else 0

            query = f'''
                SELECT er.id, er."requestId", er."villaId", er."customerId", er.category,
                       er.description, er.images, er."serviceType", er."requestStatus",
                      er."teamLeaderId", er."supervisorId", er."assignedTime", er."closedTime", er."paymentUpdatedAt", er."responseTime", er."resolutionTime",
                       er."paymentStatus", er."paymentId", er."transactionId", er."hashId",
                       er."rawUrl", er.amount, er."createdAt", er."updatedAt","issueName",
                       c."customerName", v."villaName",
                      tl."userName" AS "teamLeaderUserName", tl."fullName" AS "teamLeaderName",
                      su."userName" AS "supervisorUserName", su."fullName" AS "supervisorName"
                FROM public."emergencyRequest" er
                LEFT JOIN public."customer" c ON er."customerId" = c."id"
                LEFT JOIN public."villaDetails" v ON er."villaId" = v."id"
                  LEFT JOIN public."user" tl ON er."teamLeaderId" = tl."id"
                  LEFT JOIN public."user" su ON er."supervisorId" = su."id"

                {where_clause}
                ORDER BY er."{sort_field}" {order_dir}
            '''

            query_params = list(params)
            if not is_export and page_size > 0:
                query += ' LIMIT %s OFFSET %s'
                query_params.extend([page_size, (page - 1) * page_size])

            results = execute_query(query, query_params, many=True)
            rows = results if isinstance(results, list) else []

            for row in rows:
                self._serialize_request_row(request, row)

            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 and not is_export else 1
            response_data = {
                "results": rows,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page if not is_export else 1,
                    "pageSize": page_size if not is_export else total_count,
                }
            }

            return success_response(data=response_data, message='Emergency requests fetched successfully.', status_code=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f'Error fetching emergency requests: {str(e)}', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def put(self, request, request_id):
        try:
            if not request_id:
                return error_response(message='requestId is required.', status_code=status.HTTP_400_BAD_REQUEST)

            data = getattr(request, 'data', None) or request.POST
            request_status = data.get('requestStatus')
            team_leader_id = data.get('teamLeaderId')
            description_ = data.get('description')
            supervisor_id = data.get('supervisorId')
            payment_status = data.get('paymentStatus')
            payment_id = data.get('paymentId')
            transaction_id = data.get('transactionId')
            hash_id = data.get('hash') or data.get('hashId')
            raw_url = data.get('rawUrl')
            issue_name = data.get('issueName', '').strip()

            existing_query = '''
                SELECT id, "requestId", "customerId", "createdAt", "description", "requestStatus", "teamLeaderId", "supervisorId", "assignedTime", "closedTime", images, amount, "issueName"
                FROM public."emergencyRequest"
                WHERE "requestId" = %s AND COALESCE("isDeleted", 0) = 0
                LIMIT 1
            '''
            existing = execute_query(existing_query, [request_id], fetch='one')
            if isinstance(existing, list) and existing:
                existing = existing[0]

            if not existing:
                return error_response(message='Emergency request not found.', status_code=status.HTTP_404_NOT_FOUND)

            update_fields = []
            update_params = []
            audit_actions = []

            request_status_value = None
            team_leader_id_value = None
            supervisor_id_value = None
            description_value = None
            issue_name_value = None
            
            # Valid emergency request statuses (ALL_EXCEPT_CLOSED is a filter constant, not a valid storage value)
            VALID_EMERGENCY_REQUEST_STATUSES = [OPEN, CLOSED, ON_HOLD, IN_PROGRESS, AWAITING_GATE_PASS, QUOTATION_STAGE, JOB_APPROVED, CANCELLED]
            
            if request_status not in (None, ''):
                try:
                    request_status_value = int(request_status)
                except (TypeError, ValueError):
                    return error_response(message='requestStatus must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)
                
                if request_status_value not in VALID_EMERGENCY_REQUEST_STATUSES:
                    return error_response(message=f'requestStatus must be one of {VALID_EMERGENCY_REQUEST_STATUSES}.', status_code=status.HTTP_400_BAD_REQUEST)

            if team_leader_id not in (None, ''):
                try:
                    team_leader_id_value = int(team_leader_id)
                except (TypeError, ValueError):
                    return error_response(message='teamLeaderId must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)

            if supervisor_id not in (None, ''):
                try:
                    supervisor_id_value = int(supervisor_id)
                except (TypeError, ValueError):
                    return error_response(message='supervisorId must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)

            if description_ not in (None, ''):
                description_value = description_

            if issue_name not in (None, ''):
                issue_name_value = issue_name

            team_leader_user = None
            supervisor_user = None
            if team_leader_id_value is not None:
                team_leader_user = self._get_user_details(team_leader_id_value)
                if not team_leader_user:
                    return error_response(message='teamLeaderId user not found.', status_code=status.HTTP_404_NOT_FOUND)

            if supervisor_id_value is not None:
                supervisor_user = self._get_user_details(supervisor_id_value)
                if not supervisor_user:
                    return error_response(message='supervisorId user not found.', status_code=status.HTTP_404_NOT_FOUND)

            if request_status_value is not None or team_leader_id_value is not None or supervisor_id_value is not None or description_value is not None or issue_name_value is not None:
                if request_status_value is None:
                    request_status_value = existing.get('requestStatus')

                if request_status_value == IN_PROGRESS and team_leader_id_value is None and not existing.get('teamLeaderId'):
                    return error_response(message='teamLeaderId is required when assigning an emergency request.', status_code=status.HTTP_400_BAD_REQUEST)

                if team_leader_id_value is not None:
                    update_fields.append('"teamLeaderId" = %s')
                    update_params.append(team_leader_id_value)

                if supervisor_id_value is not None:
                    update_fields.append('"supervisorId" = %s')
                    update_params.append(supervisor_id_value)
                    if not existing.get('assignedTime'):
                        update_fields.append('"assignedTime" = NOW()')
                        update_fields.append('"responseTime" = ROUND(EXTRACT(EPOCH FROM (NOW() - "createdAt")) / 60.0, 2)')
                        audit_actions.append('Assign')

                if description_value is not None:
                    update_fields.append('"description" = %s')
                    update_params.append(description_value)

                if issue_name_value is not None:
                    update_fields.append('"issueName" = %s')
                    update_params.append(issue_name_value)

                update_fields.append('"requestStatus" = %s')
                update_params.append(request_status_value)
        
                if request_status_value == CLOSED:
                    if not existing.get('closedTime'):
                        update_fields.append('"closedTime" = NOW()')
                        update_fields.append('"resolutionTime" = ROUND(EXTRACT(EPOCH FROM (NOW() - "createdAt")) / 60.0, 2)')
                    audit_actions.append('Close')
                else:
                    audit_actions.append('StatusUpdate')
                    # Clear closedTime if reopening a closed request
                    if existing.get('requestStatus') == CLOSED:
                        update_fields.append('"closedTime" = NULL')
                        audit_actions.append('Reopen')

            payment_update_requested = any(value not in (None, '') for value in [payment_status, payment_id, transaction_id, hash_id, raw_url])
            if payment_update_requested:
                if payment_status in (None, ''):
                    return error_response(message='paymentStatus is required when updating payment fields.', status_code=status.HTTP_400_BAD_REQUEST)

                try:
                    payment_status = int(payment_status)
                except (TypeError, ValueError):
                    return error_response(message='paymentStatus must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)

                if payment_status not in (PAYMENT_SUCCESS, PAYMENT_FAILED):
                    return error_response(message='paymentStatus must be 0 for success or 1 for failed.', status_code=status.HTTP_400_BAD_REQUEST)

                payment_fields = [
                        '"paymentStatus" = %s',
                        '"paymentId" = %s',
                        '"transactionId" = %s',
                        '"hashId" = %s',
                        '"rawUrl" = %s',
                        '"paymentUpdatedAt" = NOW()',
                    ]
                payment_params = [payment_status, payment_id, transaction_id, hash_id, raw_url]

                    # Only update description/issueName when provided to avoid overwriting existing values with NULL
                if description_value is not None:
                    payment_fields.insert(3, '"description" = %s')
                    payment_params.insert(3, description_value)
                if issue_name_value is not None:
                        # place after description if present, otherwise after transactionId
                    insert_idx = 4 if description_value is not None else 3
                    payment_fields.insert(insert_idx, '"issueName" = %s')
                    payment_params.insert(insert_idx, issue_name_value)

                update_fields.extend(payment_fields)
                update_params.extend(payment_params)

            if not update_fields:
                return error_response(message='No valid update fields provided.', status_code=status.HTTP_400_BAD_REQUEST)

            update_fields.append('"updatedAt" = NOW()')
            update_query = f'''
                UPDATE public."emergencyRequest"
                SET {", ".join(update_fields)}
                WHERE "requestId" = %s AND COALESCE("isDeleted", 0) = 0
                RETURNING id, "requestId", "customerId", category, description, images, "issueName",
                          "serviceType", "requestStatus", "teamLeaderId", "supervisorId", "assignedTime", "closedTime", "responseTime", "resolutionTime",
                          "paymentStatus", "paymentId", "paymentUpdatedAt", "transactionId", "hashId", "rawUrl", amount, "createdAt", "updatedAt"
            '''

            updated = execute_query(update_query, update_params + [request_id], fetch='one')
            if isinstance(updated, list) and updated:
                updated = updated[0]

            if not updated:
                if payment_update_requested:
                    log_activity_raw(
                        request=request,
                        category='EmergencyRequest',
                        action='PaymentUpdateFailed',
                        performer=getattr(request, 'user', None),
                        details={
                            'requestId': request_id,
                            'paymentStatus': payment_status,
                            'transactionId': transaction_id,
                            'hashId': hash_id,
                            'rawUrl': raw_url,
                        }
                    )
                return error_response(message='Failed to update emergency request.', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

            previous_status = existing.get('requestStatus')
            previous_supervisor_id = existing.get('supervisorId')

            updated = self._serialize_request_row(request, updated)
            if updated.get('teamLeaderId'):
                if not team_leader_user:
                    team_leader_user = self._get_user_details(updated.get('teamLeaderId'))
                if team_leader_user:
                    updated['teamLeaderName'] = team_leader_user.get('fullName') or team_leader_user.get('userName')
                    updated['teamLeaderUserName'] = team_leader_user.get('userName')
            if updated.get('supervisorId'):
                if not supervisor_user:
                    supervisor_user = self._get_user_details(updated.get('supervisorId'))
                if supervisor_user:
                    updated['supervisorName'] = supervisor_user.get('fullName') or supervisor_user.get('userName')
                    updated['supervisorUserName'] = supervisor_user.get('userName')

            # Matrix-based in-app/push notifications
            try:
                request_id_str = str(updated.get('requestId'))
                customer_id = updated.get('customerId')
                current_status = updated.get('requestStatus')
                current_supervisor_id = updated.get('supervisorId')

                # Event: Payment Completed -> Customer + Office Admin
                if payment_update_requested and payment_status == PAYMENT_SUCCESS:
                    payload = {'requestId': request_id_str}
                    if customer_id:
                        self._notify_customer(
                            customer_id=customer_id,
                            title=f"Emergency Request #{request_id_str}",
                            body=f"Emergency Request #{request_id_str} has been opened for review.",
                            notification_type='EMERGENCY_PAYMENT_COMPLETED_CUSTOMER',
                            data_payload=payload,
                        )
                    office_admins = self._get_users_by_group_numbers([ROLE_GROUP_OFFICE_ADMIN])
                    self._notify_users(
                        users=office_admins,
                        title=f"Emergency Request #{request_id_str}",
                        body=f"New Emergency Request #{request_id_str} has been registered.",
                        notification_type='EMERGENCY_PAYMENT_COMPLETED_ADMIN',
                        data_payload=payload,
                    )

                # Event: Supervisor Assigned -> Supervisor
                if current_supervisor_id and str(current_supervisor_id) != str(previous_supervisor_id or ""):
                    target_supervisor = self._get_user_notification_target(current_supervisor_id)
                    if target_supervisor:
                        self._notify_users(
                            users=[target_supervisor],
                            title=f"Emergency Request #{request_id_str}",
                            body=f"Emergency Request #{request_id_str} has been assigned to you.",
                            notification_type='EMERGENCY_SUPERVISOR_ASSIGNED',
                            data_payload={'requestId': request_id_str},
                        )

                # Status-change notifications
                status_changed = str(current_status) != str(previous_status)
                if status_changed:
                    if current_status == QUOTATION_STAGE:
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_str}",
                                body=f"Emergency Request #{request_id_str} is now in quotation stage.",
                                notification_type='EMERGENCY_QUOTATION_STAGE_CUSTOMER',
                                data_payload={'requestId': request_id_str},
                            )
                        estimators = self._get_estimator_users()
                        self._notify_users(
                            users=estimators,
                            title=f"Emergency Request #{request_id_str}",
                            body=f"Quotation required for Emergency Request #{request_id_str}.",
                            notification_type='EMERGENCY_QUOTATION_REQUIRED',
                            data_payload={'requestId': request_id_str},
                        )

                    elif current_status == IN_PROGRESS:
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_str}",
                                body=f"Emergency Request #{request_id_str} is currently in progress.",
                                notification_type='EMERGENCY_IN_PROGRESS_CUSTOMER',
                                data_payload={'requestId': request_id_str},
                            )

                    elif current_status == JOB_APPROVED:
                        if current_supervisor_id:
                            target_supervisor = self._get_user_notification_target(current_supervisor_id)
                            if target_supervisor:
                                self._notify_users(
                                    users=[target_supervisor],
                                    title=f"Emergency Request #{request_id_str}",
                                    body=f"Emergency Request #{request_id_str} has been approved. Proceed with the work.",
                                    notification_type='EMERGENCY_JOB_APPROVED_SUPERVISOR',
                                    data_payload={'requestId': request_id_str},
                                )
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_str}",
                                body=f"Emergency Request #{request_id_str} is in progress and will be resolved as soon as possible.",
                                notification_type='EMERGENCY_JOB_APPROVED_CUSTOMER',
                                data_payload={'requestId': request_id_str},
                            )

                    elif current_status == CLOSED:
                        if customer_id:
                            self._notify_customer(
                                customer_id=customer_id,
                                title=f"Emergency Request #{request_id_str}",
                                body=f"Emergency Request #{request_id_str} has been completed successfully.",
                                notification_type='EMERGENCY_CLOSED_CUSTOMER',
                                data_payload={'requestId': request_id_str},
                            )
                        office_admins = self._get_users_by_group_numbers([ROLE_GROUP_OFFICE_ADMIN])
                        self._notify_users(
                            users=office_admins,
                            title=f"Emergency Request #{request_id_str}",
                            body=f"Emergency Request #{request_id_str} has been completed and closed.",
                            notification_type='EMERGENCY_CLOSED_ADMIN',
                            data_payload={'requestId': request_id_str},
                        )
            except Exception:
                pass

            # Send payment status emails to customer and office admin when payment is updated
            if payment_update_requested:
                try:
                    self._send_payment_status_emails(
                        updated=updated,
                        payment_status=payment_status,
                        payment_id=payment_id,
                        transaction_id=transaction_id,
                    )
                except Exception:
                    pass

            if 'Assign' in audit_actions:
                log_activity_raw(
                    request=request,
                    category='EmergencyRequest',
                    action='Assign',
                    performer=getattr(request, 'user', None),
                    details={
                        'requestId': request_id,
                        'teamLeaderId': updated.get('teamLeaderId'),
                        'teamLeaderName': updated.get('teamLeaderName'),
                        'supervisorId': updated.get('supervisorId'),
                        'supervisorName': updated.get('supervisorName'),
                        'assignedTime': updated.get('assignedTime'),
                        'requestStatus': self._get_status_name(updated.get('requestStatus')),
                        'issueName': updated.get('issueName'),
                    }
                )

            if 'Close' in audit_actions:
                log_activity_raw(
                    request=request,
                    category='EmergencyRequest',
                    action='Close',
                    performer=getattr(request, 'user', None),
                    details={
                        'requestId': request_id,
                        'closedTime': updated.get('closedTime'),
                        'requestStatus': self._get_status_name(updated.get('requestStatus')),
                        'issueName': updated.get('issueName'),
                    }
                )

            if payment_update_requested:
                payment_status_text = 'Success' if payment_status == PAYMENT_SUCCESS else 'Failed'
                log_activity_raw(
                    request=request,
                    category='EmergencyRequest',
                    action='PaymentUpdate',
                    performer=getattr(request, 'user', None),
                    details={
                        'requestId': request_id,
                        'paymentStatus': payment_status_text,
                        'transactionId': transaction_id,
                        'hashId': hash_id,
                        'rawUrl': raw_url,
                        'issueName': updated.get('issueName'),
                    }
                )
            elif 'StatusUpdate' in audit_actions:
                log_activity_raw(
                    request=request,
                    category='EmergencyRequest',
                    action='StatusUpdate',
                    performer=getattr(request, 'user', None),
                    details={
                        'requestId': request_id,
                        'requestStatus': self._get_status_name(updated.get('requestStatus')),
                        'issueName': updated.get('issueName'),
                    }
                )

            response_message = 'Emergency request payment updated successfully.' if payment_update_requested and not audit_actions else 'Emergency request updated successfully.'

            return success_response(
                data=updated,
                message=response_message,
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f'Error updating emergency request: {str(e)}', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


class EmergencyRequestStatsView(EmergencyRequestView):
    def get(self, request):
        try:
            start_date = (request.query_params.get('startDate') or '').strip()
            end_date = (request.query_params.get('endDate') or '').strip()

            if not start_date or not end_date:
                return error_response(message='startDate and endDate are required in YYYY-MM-DD format.', status_code=status.HTTP_400_BAD_REQUEST)

            try:
                start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
                end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()
            except ValueError:
                return error_response(message='startDate and endDate must be valid dates in YYYY-MM-DD format.', status_code=status.HTTP_400_BAD_REQUEST)

            if start_date_obj > end_date_obj:
                return error_response(message='startDate cannot be after endDate.', status_code=status.HTTP_400_BAD_REQUEST)

            period_days = (end_date_obj - start_date_obj).days + 1
            previous_end_date = start_date_obj - timedelta(days=1)
            previous_start_date = previous_end_date - timedelta(days=period_days - 1)

            # Use the selected end date as the "today" reference so stats are
            # relative to the requested period rather than the server's current date.
            today_date = end_date_obj
            yesterday_date = end_date_obj - timedelta(days=1)

            query = '''
                SELECT
                    ROUND(COALESCE(SUM(CASE
                        WHEN er."paymentStatus" = %s
                         AND COALESCE(er."paymentUpdatedAt", er."updatedAt", er."createdAt")::date BETWEEN %s AND %s
                        THEN COALESCE(er.amount, 0)
                        ELSE 0
                    END), 0)::numeric, 2) AS "totalCollected",
                    ROUND(COALESCE(SUM(CASE
                        WHEN er."paymentStatus" = %s
                         AND COALESCE(er."paymentUpdatedAt", er."updatedAt", er."createdAt")::date BETWEEN %s AND %s
                        THEN COALESCE(er.amount, 0)
                        ELSE 0
                    END), 0)::numeric, 2) AS "previousTotalCollected",
                    COUNT(*) FILTER (
                        WHERE er."createdAt"::date BETWEEN %s AND %s
                          AND er."closedTime" IS NULL
                          AND er."paymentStatus" = %s
                    ) AS "activeRequests",
                    COUNT(*) FILTER (
                        WHERE er."createdAt"::date = %s
                          AND er."paymentStatus" = %s
                    ) AS "activeRequestsNewToday",
                    COUNT(*) FILTER (
                        WHERE er."closedTime" IS NOT NULL
                          AND er."closedTime"::date BETWEEN %s AND %s
                    ) AS "closedRequests",
                    ROUND(COALESCE(AVG(
                        CASE
                            WHEN er."closedTime" IS NOT NULL
                             AND er."closedTime"::date BETWEEN %s AND %s
                             AND er."closedTime" >= er."createdAt"
                            THEN EXTRACT(EPOCH FROM (er."closedTime" - er."createdAt")) / 60.0
                            ELSE NULL
                        END
                    ), 0)::numeric, 2) AS "closedAvgResolutionMinutes",
                    COUNT(*) FILTER (
                        WHERE er."paymentStatus" = %s
                          AND COALESCE(er."paymentUpdatedAt", er."updatedAt", er."createdAt")::date BETWEEN %s AND %s
                    ) AS "failedPayments",
                    COUNT(*) FILTER (
                        WHERE er."paymentStatus" = %s
                          AND COALESCE(er."paymentUpdatedAt", er."updatedAt", er."createdAt")::date = %s
                    ) AS "failedPaymentsToday",
                    COUNT(*) FILTER (
                        WHERE er."paymentStatus" = %s
                          AND COALESCE(er."paymentUpdatedAt", er."updatedAt", er."createdAt")::date = %s
                    ) AS "failedPaymentsYesterday",
                    ROUND(COALESCE(AVG(
                        CASE
                            WHEN er."createdAt"::date BETWEEN %s AND %s
                             AND er."closedTime" IS NOT NULL
                             AND er."closedTime" >= er."createdAt"
                            THEN EXTRACT(EPOCH FROM (er."closedTime" - er."createdAt")) / 60.0
                            ELSE NULL
                        END
                    ), 0)::numeric, 2) AS "avgResolutionTime"
                FROM public."emergencyRequest" er
                WHERE COALESCE(er."isDeleted", 0) = 0
            '''
            params = [
                PAYMENT_SUCCESS, start_date_obj, end_date_obj,
                PAYMENT_SUCCESS, previous_start_date, previous_end_date,
                start_date_obj, end_date_obj, PAYMENT_SUCCESS,
                today_date, PAYMENT_SUCCESS,
                start_date_obj, end_date_obj,
                start_date_obj, end_date_obj,
                PAYMENT_FAILED, start_date_obj, end_date_obj,
                PAYMENT_FAILED, today_date,
                PAYMENT_FAILED, yesterday_date,
                start_date_obj, end_date_obj,
            ]
            result = execute_query(query, params, fetch='one')
            if isinstance(result, list) and result:
                result = result[0]

            if not isinstance(result, dict):
                result = {}

            total_collected = float(result.get('totalCollected') or 0)
            previous_total_collected = float(result.get('previousTotalCollected') or 0)
            active_requests = int(result.get('activeRequests') or 0)
            closed_requests = int(result.get('closedRequests') or 0)
            failed_payments = int(result.get('failedPayments') or 0)
            avg_resolution_time = float(result.get('avgResolutionTime') or 0)
            active_requests_new_today = int(result.get('activeRequestsNewToday') or 0)
            closed_avg_resolution_minutes = float(result.get('closedAvgResolutionMinutes') or 0)
            failed_payments_today = int(result.get('failedPaymentsToday') or 0)
            failed_payments_yesterday = int(result.get('failedPaymentsYesterday') or 0)

            if previous_total_collected > 0:
                total_collected_change_pct = round(((total_collected - previous_total_collected) / previous_total_collected) * 100.0, 2)
            elif total_collected > 0:
                total_collected_change_pct = 100.0
            else:
                total_collected_change_pct = 0.0

            failed_payments_vs_yesterday = failed_payments_today - failed_payments_yesterday

            try:
                sla_target_minutes = int(request.query_params.get('slaTargetMinutes', 45) or 45)
            except (TypeError, ValueError):
                return error_response(message='slaTargetMinutes must be an integer.', status_code=status.HTTP_400_BAD_REQUEST)
            resolution_time_delta_vs_sla = round(avg_resolution_time - sla_target_minutes, 2)

            response_payload = {
                'startDate': start_date,
                'endDate': end_date,
                'previousStartDate': previous_start_date.isoformat(),
                'previousEndDate': previous_end_date.isoformat(),
                'totalCollected': total_collected,
                'activeRequests': active_requests,
                'closedRequests': closed_requests,
                'failedPayments': failed_payments,
                'avgResolutionTime': avg_resolution_time,
                'calculation': {
                    'totalCollectedChangePct': total_collected_change_pct,
                    'activeRequestsNewToday': active_requests_new_today,
                    'closedAvgResolutionMinutes': closed_avg_resolution_minutes,
                    'failedPaymentsYesterday': failed_payments_yesterday,
                    'failedPaymentsVsYesterday': failed_payments_vs_yesterday,
                    'slaTargetMinutes': sla_target_minutes,
                    'resolutionTimeDeltaVsSla': resolution_time_delta_vs_sla,
                }
            }

            return success_response(data=response_payload, message='Emergency request stats fetched successfully.', status_code=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f'Error fetching emergency request stats: {str(e)}', status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
