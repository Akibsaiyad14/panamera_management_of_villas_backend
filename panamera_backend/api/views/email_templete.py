from rest_framework.views import APIView
from rest_framework import status
from django.db import transaction
from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from api.utils import execute_query, success_response, error_response, log_activity_raw

class MailTemplateView(APIView):

    permission_classes = [IsAuthenticated]


    def get(self, request):
        """
        Fetch all active mail templates (isDeleted = 0)
        """
        try:
            query = """
                SELECT id, "templateName", subject, body
                FROM "mailTemplates"
                WHERE "isDeleted" = 0
                ORDER BY id DESC
            """
            result = execute_query(query, many=True)

            # Convert escaped \n into real line breaks
            for row in result:
                if row.get("body"):
                    row["body"] = row["body"].replace("\n\n", "\n")  # real newlines

            return success_response(result, "Templates fetched successfully")
        except Exception as e:
            return error_response(f"Error fetching templates: {str(e)}", status.HTTP_500_INTERNAL_SERVER_ERROR)


    def post(self, request):
        """
        Create a new mail template
        """
        try:
            data = request.data
            required_fields = ["templateName", "subject", "body"]
            missing = [f for f in required_fields if not data.get(f)]
            if missing:
                return error_response(f"Missing fields: {', '.join(missing)}", status.HTTP_400_BAD_REQUEST)

            insert_query = """
                INSERT INTO "mailTemplates" ("templateName", subject, body, "isDeleted")
                VALUES (%s, %s, %s, 0)
                RETURNING id
            """
            params = [data["templateName"], data["subject"], data["body"]]
            result = execute_query(insert_query, params, fetch="one")

            log_activity_raw(
                request=request,
                category='MailSetting',
                action='Add',
                performer=request.user,
                details={'title': data["templateName"]}
            )

            return success_response({"id": result[0]}, "Template created successfully", status.HTTP_201_CREATED)
        except Exception as e:
            return error_response(f"Error creating template: {str(e)}", status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, template_id):
        """
        Update existing template
        """
        try:
            data = request.data
            with transaction.atomic():
                update_query = """
                    UPDATE "mailTemplates"
                    SET "templateName"=%s, subject=%s, body=%s
                    WHERE id=%s AND "isDeleted" = 0
                """
                params = [
                    data.get("templateName"),
                    data.get("subject"),
                    data.get("body"),
                    template_id
                ]
                execute_query(update_query, params)

            log_activity_raw(
                request=request,
                category='MailTemplate',
                action='Update',
                performer=request.user,
                details={'title': data.get("templateName", 'N/A')}
            )


            return success_response(None, "Template updated successfully", status.HTTP_200_OK)
        except Exception as e:
            return error_response(f"Error updating template: {str(e)}", status.HTTP_500_INTERNAL_SERVER_ERROR)
