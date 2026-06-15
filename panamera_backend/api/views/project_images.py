import os
import json
import traceback
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, save_project_images


class ProjectImagesView(APIView):
    """
    CRUD API for projectImages table.
    Stores images in MEDIA folder and returns absolute URLs.
    """
    # permission_classes = [IsAuthenticated]

    def _process_image_path(self, request, path):
        """Convert relative image path into full URL."""
        if not path:
            return None
        return request.build_absolute_uri(settings.MEDIA_URL + path)


    def get(self, request, image_id=None):
        """
        Get single image (by id) or list all.
        """
        try:
            if image_id:
                query = 'SELECT id, "imagePath" AS "imageUrl" FROM "projectImages" WHERE id = %s;'
                result = execute_query(query, [image_id], fetch='one')
                if not result:
                    return error_response(message="Image not found.", status_code=status.HTTP_404_NOT_FOUND)

                result['imageUrl'] = self._process_image_path(request, result['imageUrl'])
                return success_response(data=result, message="Image retrieved successfully.", status_code=status.HTTP_200_OK)

            # list all
            query = 'SELECT id, "imagePath" AS "imageUrl" FROM "projectImages" ORDER BY id DESC;'
            results = execute_query(query, [], many=True)
            for r in results:
                r['imageUrl'] = self._process_image_path(request, r['imageUrl'])

            return success_response(data=results, message="Images retrieved successfully.", status_code=status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error retrieving images: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



    def post(self, request):
        """
        Upload image(s) and save path(s) in DB.
        """
        try:
            image_files = request.FILES.getlist('image')
            if not image_files:
                return error_response(message="No images provided.", status_code=status.HTTP_400_BAD_REQUEST)

            image_paths = save_project_images(image_files)

            insert_query = """
                INSERT INTO "projectImages" ("imagePath")
                VALUES (%s)
                RETURNING id, "imagePath" AS "imageUrl";
            """
            inserted = []
            for path in image_paths:
                result = execute_query(insert_query, [path], fetch='one')

                # Handle both dict and list return types
                if isinstance(result, dict):
                    r = result
                elif isinstance(result, list) and result:
                    r = result[0]
                else:
                    continue

                r['imageUrl'] = self._process_image_path(request, r.get('imageUrl'))
                inserted.append(r)

            return success_response(data=inserted, message="Images uploaded successfully.", status_code=status.HTTP_201_CREATED)

        except Exception as e:
            traceback.print_exc()
            return error_response(message=f"Error uploading images: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def delete(self, request, image_id=None):
        """
        Delete an image by ID.
        Removes DB row and deletes physical file from MEDIA folder.
        """
        try:
            if not image_id:
                return error_response("image_id required.", status.HTTP_400_BAD_REQUEST)

            # Get image path first
            check_query = 'SELECT "imagePath" FROM "projectImages" WHERE id = %s;'
            existing = execute_query(check_query, [image_id], fetch='one')
            if not existing:
                return error_response("Image not found.", status.HTTP_404_NOT_FOUND)

            # Handle dict/list return type
            if isinstance(existing, dict):
                image_path = existing.get("imagePath")
            elif isinstance(existing, list) and existing:
                image_path = existing[0].get("imagePath")
            else:
                image_path = None

            # Delete row from DB
            delete_query = 'DELETE FROM "projectImages" WHERE id = %s RETURNING id;'
            deleted = execute_query(delete_query, [image_id], fetch='one')
            if not deleted:
                return error_response("Failed to delete image.", status.HTTP_400_BAD_REQUEST)

            # Delete physical file if exists
            if image_path:
                file_path = os.path.join(settings.MEDIA_ROOT, image_path.replace(settings.MEDIA_URL, "").lstrip("/"))
                if os.path.exists(file_path):
                    os.remove(file_path)

            return success_response({"id": image_id}, "Image deleted successfully.", status.HTTP_200_OK)

        except Exception as e:
            traceback.print_exc()
            return error_response(f"Error deleting image: {str(e)}", status.HTTP_500_INTERNAL_SERVER_ERROR)
