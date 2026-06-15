from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import execute_query, success_response, error_response
from api.messages import (
    CUSTOMERS_FETCHED_SUCCESSFULLY,
    CUSTOMERS_WITH_AVAILABLE_AMC_SLOTS_FETCHED_SUCCESSFULLY,
)
from collections import OrderedDict


class CustomerListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """
        Fetches a list of customers with available AMC slots (fewer than 3 active AMCs),
        excluding villas already assigned in AMCMaster. If all villas are assigned,
        the customer is excluded. If getAll=true, returns all customers and their villas.
        """
        try:
            get_all = request.query_params.get("getAll", "false").lower() == "true"

            if get_all:
                # Return all customers with their villas (no AMC filters)
                query = """
                    SELECT
                        c.id, c."customerId", c."customerName", c.emirate, c."contactNumber", c.email, c.status,
                        v.id AS villa_id, v."villaName", v.community
                    FROM customer c
                    INNER JOIN "villaDetails" v ON c.id = v."customerId" AND v."isDeleted" = 0
                    WHERE c."isDeleted" = 0
                    ORDER BY c."customerName", c.id;
                """
            else:
                # Apply AMC filters — exclude villas already in AMCMaster, exclude customers with all villas assigned
                query = """
                    WITH assigned_villas AS (
                        SELECT DISTINCT "villaId"
                        FROM "AMCMaster"
                        WHERE "isDeleted" = 0
                    )
                    SELECT
                        c.id, c."customerId", c."customerName", c.emirate, c."contactNumber", c.email, c.status,
                        v.id AS villa_id, v."villaName", v.community
                    FROM customer c
                    INNER JOIN "villaDetails" v 
                        ON c.id = v."customerId" AND v."isDeleted" = 0
                    LEFT JOIN assigned_villas av 
                        ON v.id = av."villaId"
                    WHERE 
                        c."isDeleted" = 0
                        AND av."villaId" IS NULL -- villa not assigned in AMCMaster
                        AND EXISTS ( -- ensure at least one villa is unassigned for this customer
                            SELECT 1
                            FROM "villaDetails" vv
                            LEFT JOIN assigned_villas avv ON vv.id = avv."villaId"
                            WHERE vv."customerId" = c.id AND vv."isDeleted" = 0 AND avv."villaId" IS NULL
                        )
                    ORDER BY c."customerName", c.id;
                """

            results = execute_query(query, many=True)

            customers_map = OrderedDict()
            columns = [
                "id", "customerId", "customerName", "emirate", "contactNumber", "email", "status",
                "villa_id", "villaName", "community"
            ]

            dict_results = [dict(zip(columns, row)) if not isinstance(row, dict) else row for row in results]

            for row in dict_results:
                customer_id = row["id"]
                if customer_id not in customers_map:
                    customers_map[customer_id] = {
                        "id": row["id"],
                        "customerId": row["customerId"],
                        "customerName": row["customerName"],
                        "emirate": row["emirate"],
                        "contactNumber": row["contactNumber"],
                        "email": row["email"],
                        "status": row["status"],
                        "villaList": []
                    }

                if row.get("villa_id"):
                    customers_map[customer_id]["villaList"].append({
                        "villaId": row["villa_id"],
                        "villaName": row["villaName"],
                        "community": row["community"]
                    })

            transformed_customers = list(customers_map.values())

            return success_response(
                data=transformed_customers,
                message=CUSTOMERS_FETCHED_SUCCESSFULLY if get_all else CUSTOMERS_WITH_AVAILABLE_AMC_SLOTS_FETCHED_SUCCESSFULLY,
                status_code=status.HTTP_200_OK
            )

        except Exception as e:
            return error_response(message=f"Error fetching customers: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
