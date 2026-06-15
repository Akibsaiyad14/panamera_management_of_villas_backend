import traceback
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from ..utils import error_response, success_response, execute_query, log_activity_raw


# ---------------------------------------------------------------------------
# Allowed sort fields (whitelist to prevent SQL injection)
# ---------------------------------------------------------------------------
ALLOWED_SORT_FIELDS = {
    "stockCode", "description", "unit", "id",
}


class StockMaterialView(APIView):
    permission_classes = [IsAuthenticated]

    # ------------------------------------------------------------------
    # GET — Fetch one (by id) or list with search, sort, filter, pagination
    # ------------------------------------------------------------------
    def get(self, request, stock_id=None):
        try:
            # ---- single fetch ----
            if stock_id:
                row = execute_query(
                    '''SELECT id, "stockCode", description, unit,
                       FROM public."stockMaterial"
                       WHERE id = %s AND COALESCE("isDeleted", 0) = 0
                       LIMIT 1''',
                    [stock_id], fetch="one",
                )
                if isinstance(row, list):
                    row = row[0] if row else None
                if not row:
                    return error_response(
                        message="Stock material not found.",
                        status_code=status.HTTP_404_NOT_FOUND,
                    )
                return success_response(
                    data=row,
                    message="Stock material fetched successfully.",
                )

            # ---- list fetch with pagination, search, sort, filter ----
            search       = request.query_params.get("search", "").strip()
            unit_filter  = request.query_params.get("unit", "").strip()
            sort_param   = request.query_params.get("sort", "stockCode").strip() or "stockCode"
            order_param  = request.query_params.get("order", "asc").strip().lower() or "asc"
            page         = int(request.query_params.get("page", 1))
            page_size    = int(request.query_params.get("pageSize", 20))
            is_export    = request.query_params.get("isExport", "false").lower() == "true"

            where_conditions = ['COALESCE("isDeleted", 0) = 0']
            params = []

            if search:
                where_conditions.append('''
                    (
                        "stockCode"   ILIKE %s OR
                        description   ILIKE %s OR
                        unit          ILIKE %s
                    )
                ''')
                sp = f"%{search}%"
                params.extend([sp, sp, sp])

            if unit_filter:
                where_conditions.append('unit ILIKE %s')
                params.append(f"%{unit_filter}%")

            sort_field = sort_param if sort_param in ALLOWED_SORT_FIELDS else "stockCode"
            order_dir  = "asc" if order_param == "asc" else "desc"

            where_clause = "WHERE " + " AND ".join(where_conditions)

            # ---- count ----
            count_result = execute_query(
                f'''SELECT COUNT(*) AS total
                    FROM public."stockMaterial"
                    {where_clause}''',
                params, fetch="one",
            )
            if isinstance(count_result, list):
                count_result = count_result[0] if count_result else {}
            total_count = int((count_result or {}).get("total") or 0)

            # ---- fetch rows ----
            list_sql = f'''
                SELECT id, "stockCode", description, unit,
                       "createdAt", "updatedAt"
                FROM public."stockMaterial"
                {where_clause}
                ORDER BY "{sort_field}" {order_dir}
            '''
            query_params = list(params)
            if not is_export and page_size > 0:
                list_sql += " LIMIT %s OFFSET %s"
                query_params.extend([page_size, (page - 1) * page_size])

            rows = execute_query(list_sql, query_params, many=True) or []

            total_pages = (
                (total_count + page_size - 1) // page_size if page_size > 0 and not is_export else 1
            )

            return success_response(
                data={
                    "results": rows,
                    "pagination": {
                        "totalRecords": total_count,
                        "totalPages": total_pages,
                        "currentPage": page if not is_export else 1,
                        "pageSize": page_size if not is_export else total_count,
                    },
                },
                message="Stock materials fetched successfully.",
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error fetching stock materials: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # POST — Create a new stock material
    # ------------------------------------------------------------------
    def post(self, request):
        try:
            data = getattr(request, "data", None) or request.POST

            stock_code  = (data.get("stockCode") or "").strip()
            description = (data.get("description") or "").strip()
            unit        = (data.get("unit") or "").strip()

            if not stock_code:
                return error_response(
                    message="stockCode is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not description:
                return error_response(
                    message="description is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            if not unit:
                return error_response(
                    message="unit is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            # Check for duplicate stockCode
            existing = execute_query(
                '''SELECT id FROM public."stockMaterial"
                   WHERE "stockCode" = %s AND COALESCE("isDeleted", 0) = 0
                   LIMIT 1''',
                [stock_code], fetch="one",
            )
            if isinstance(existing, list):
                existing = existing[0] if existing else None
            if existing:
                return error_response(
                    message="A stock material with this stockCode already exists.",
                    status_code=status.HTTP_409_CONFLICT,
                )

            inserted = execute_query(
                '''INSERT INTO public."stockMaterial"
                       ("stockCode", description, unit, "createdAt", "updatedAt", "isDeleted")
                   VALUES (%s, %s, %s, NOW(), NOW(), 0)
                   RETURNING id, "stockCode", description, unit, "createdAt", "updatedAt"''',
                [stock_code, description, unit],
                fetch="one",
            )
            if isinstance(inserted, list):
                inserted = inserted[0] if inserted else None
            if not inserted:
                return error_response(
                    message="Failed to create stock material.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            log_activity_raw(
                request=request,
                category="StockMaterial",
                action="Add",
                performer=getattr(request, "user", None),
                details={
                    "id": inserted["id"],
                    "stockCode": stock_code,
                    "description": description,
                    "unit": unit,
                },
            )

            return success_response(
                data=inserted,
                message="Stock material created successfully.",
                status_code=status.HTTP_201_CREATED,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error creating stock material: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # PUT — Update an existing stock material
    # ------------------------------------------------------------------
    def put(self, request, stock_id):
        try:
            if not stock_id:
                return error_response(
                    message="stock_id is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            data = getattr(request, "data", None) or request.POST

            # Verify record exists
            existing = execute_query(
                '''SELECT id, "stockCode" FROM public."stockMaterial"
                   WHERE id = %s AND COALESCE("isDeleted", 0) = 0
                   LIMIT 1''',
                [stock_id], fetch="one",
            )
            if isinstance(existing, list):
                existing = existing[0] if existing else None
            if not existing:
                return error_response(
                    message="Stock material not found.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            update_fields = []
            update_params = []

            # stockCode
            stock_code = data.get("stockCode")
            if stock_code not in (None, ""):
                stock_code = stock_code.strip()
                # Check uniqueness if changing stockCode
                if stock_code != existing.get("stockCode"):
                    dup = execute_query(
                        '''SELECT id FROM public."stockMaterial"
                           WHERE "stockCode" = %s AND id != %s AND COALESCE("isDeleted", 0) = 0
                           LIMIT 1''',
                        [stock_code, stock_id], fetch="one",
                    )
                    if isinstance(dup, list):
                        dup = dup[0] if dup else None
                    if dup:
                        return error_response(
                            message="Another stock material with this stockCode already exists.",
                            status_code=status.HTTP_409_CONFLICT,
                        )
                update_fields.append('"stockCode" = %s')
                update_params.append(stock_code)

            # description
            description = data.get("description")
            if description not in (None, ""):
                update_fields.append('description = %s')
                update_params.append(description.strip())

            # unit
            unit = data.get("unit")
            if unit not in (None, ""):
                update_fields.append('unit = %s')
                update_params.append(unit.strip())

            if not update_fields:
                return error_response(
                    message="No fields to update. Provide at least one of: stockCode, description, unit.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            update_fields.append('"updatedAt" = NOW()')
            updated = execute_query(
                f'''UPDATE public."stockMaterial"
                    SET {", ".join(update_fields)}
                    WHERE id = %s AND COALESCE("isDeleted", 0) = 0
                    RETURNING id, "stockCode", description, unit, "createdAt", "updatedAt"''',
                update_params + [stock_id],
                fetch="one",
            )
            if isinstance(updated, list):
                updated = updated[0] if updated else None
            if not updated:
                return error_response(
                    message="Failed to update stock material.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            log_activity_raw(
                request=request,
                category="StockMaterial",
                action="Update",
                performer=getattr(request, "user", None),
                details={"id": stock_id, "stockCode": updated.get("stockCode")},
            )

            return success_response(
                data=updated,
                message="Stock material updated successfully.",
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error updating stock material: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ------------------------------------------------------------------
    # DELETE — Soft delete one or multiple stock materials
    # ------------------------------------------------------------------
    def delete(self, request, stock_id=None):
        try:
            # Support bulk delete via query param: ?ids=1,2,3
            ids_param = request.query_params.get("ids", "").strip()

            if stock_id:
                stock_ids = [stock_id]
            elif ids_param:
                try:
                    stock_ids = [int(i.strip()) for i in ids_param.split(",") if i.strip()]
                except ValueError:
                    return error_response(
                        message="ids must be comma-separated integers.",
                        status_code=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                return error_response(
                    message="stock_id or ids query parameter is required.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            if not stock_ids:
                return error_response(
                    message="No valid IDs provided.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            placeholders = ", ".join(["%s"] * len(stock_ids))
            deleted = execute_query(
                f'''UPDATE public."stockMaterial"
                    SET "isDeleted" = 1, "updatedAt" = NOW()
                    WHERE id IN ({placeholders}) AND COALESCE("isDeleted", 0) = 0
                    RETURNING id''',
                stock_ids, fetch="all", many=True,
            )
            if not deleted:
                return error_response(
                    message="No stock materials found to delete.",
                    status_code=status.HTTP_404_NOT_FOUND,
                )

            deleted_ids = [r["id"] for r in deleted if isinstance(r, dict)]

            log_activity_raw(
                request=request,
                category="StockMaterial",
                action="Delete",
                performer=getattr(request, "user", None),
                details={"deletedIds": deleted_ids,
                         "stockCodes": [r.get("stockCode") for r in deleted if isinstance(r, dict)]},
            )

            return success_response(
                data={"deletedCount": len(deleted_ids), "deletedIds": deleted_ids},
                message=f"{len(deleted_ids)} stock material(s) deleted successfully.",
                status_code=status.HTTP_200_OK,
            )

        except Exception as e:
            traceback.print_exc()
            return error_response(
                message=f"Error deleting stock material: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
