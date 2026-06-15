import traceback
from rest_framework.views import APIView
from rest_framework import status
from api.utils import execute_query, success_response, error_response


class UploadAppVersionView(APIView):
    """
    POST → Insert new version details.
    Soft-delete old rows by setting isDeleted = 1.
    """

    def post(self, request):
        try:
            latest_version = request.data.get("latestVersion")
            min_supported = request.data.get("minSupportedVersion")
            latest_ios_version = request.data.get("latestIosVersion")
            min_supported_ios = request.data.get("minIosVersion")
            android_url = request.data.get("androidUrl")
            ios_url = request.data.get("iosUrl")

            # 1) Soft delete old rows
            execute_query(
                'UPDATE "appVersions" SET "isDeleted" = 1 WHERE "isDeleted" = 0;',
                [],
                commit=True
            )

            # 2) Insert new row as active
            insert_query = """
                INSERT INTO "appVersions"
                ("latestVersion", "minSupportedVersion", "androidUrl", "iosUrl", "isDeleted", "latestIosVersion", "minIosVersion")
                VALUES (%s, %s, %s, %s, 0, %s, %s)
                RETURNING id;
            """
            result = execute_query(
                insert_query,
                [latest_version, min_supported, android_url, ios_url, latest_ios_version, min_supported_ios],
                fetch='one'
            )

            # normalize
            if isinstance(result, dict):
                row_id = result["id"]
            elif isinstance(result, list) and result:
                row_id = result[0]["id"]
            else:
                row_id = None

            return success_response(
                data={"id": row_id},
                message="Version updated successfully.",
                status_code=status.HTTP_201_CREATED
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error updating version details: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CheckAppVersionView(APIView):
    """
    GET → Fetch only the active (isDeleted = 0) version row.
    """

    def get(self, request):
        try:
            query = """
                SELECT 
                    "latestVersion",
                    "minSupportedVersion",
                    "androidUrl",
                    "iosUrl",
                    "latestIosVersion",
                    "minIosVersion"
                FROM "appVersions"
                WHERE "isDeleted" = 0
                ORDER BY id DESC
                LIMIT 1;
            """

            result = execute_query(query, [], fetch='one')

            if not result:
                return success_response(
                    data={},
                    message="No active version found.",
                    status_code=status.HTTP_200_OK
                )

            # normalize
            if isinstance(result, list) and result:
                result = result[0]

            return success_response(
                data={
                    "latestVersion": result["latestVersion"],
                    "minSupportedVersion": result["minSupportedVersion"],
                    "androidUrl": result["androidUrl"],
                    "iosUrl": result["iosUrl"],
                    "latestIosVersion": result["latestIosVersion"],
                    "minIosVersion": result["minIosVersion"]
                },
                message="Version details fetched successfully.",
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error fetching version details: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
