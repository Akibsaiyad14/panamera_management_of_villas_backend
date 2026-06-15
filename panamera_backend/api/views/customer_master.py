from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from api.utils import success_response, error_response, execute_query, generate_password, encode_password, save_villa_images, log_activity_raw, send_mail_with_template
from api.messages import *
from api.constants import *
from django.core.mail import send_mail
from django.conf import settings
from django.db import transaction
from psycopg2.errors import UniqueViolation
from django.db.utils import IntegrityError
import json
from api.views.customer_authentication import CombinedUserCustomerAuthentication, UserCustomerPermission
from api.permission import IsUser, IsCustomer, IsUserOrCustomer
from api.models import User  # Adjust the import path if needed

class CustomerView(APIView):
    permission_classes = [IsAuthenticated]
    """
    Handles CRUD for Customers with joins to villaDetails table.
    """
    allowed_sort_fields = [
        "customerId", "customerName", "emirate", 
        "contactNumber", "email", "status"
    ]

    def get(self, request, customer_id=None):
        """
        Customer retrieval endpoint, including villaImage with base URL.
        """
        try:
            if customer_id:
                # --- FETCH A SINGLE CUSTOMER ---
                query = """
                    SELECT c.id, c."customerId", c."customerName", c.emirate, c."contactNumber", c.email, c.status, c."dateOfBirth",
                           v.id AS villa_id, v."villaName", v.community, v."villaImage"
                    FROM customer c
                    LEFT JOIN "villaDetails" v ON c.id = v."customerId" AND v."isDeleted" = 0
                    WHERE c.id = %s AND c."isDeleted" = 0
                """
                results = execute_query(query, [customer_id], many=True)
                if not results:
                    return error_response(message="Customer not found", status_code=status.HTTP_404_NOT_FOUND)
                
                # Define column names for mapping
                columns = [
                    "id", "customerId", "customerName", "emirate", "contactNumber", "email", "status", "dateOfBirth",
                    "villa_id", "villaName", "community", "villaImage"
                ]
                
                # Convert results to list of dictionaries
                dict_results = []
                for row in results:
                    if isinstance(row, list):
                        dict_results.append(dict(zip(columns, row)))
                    else:
                        dict_results.append(row)

                if not dict_results or not any(r["id"] for r in dict_results):
                    return error_response(message="Customer not found", status_code=status.HTTP_404_NOT_FOUND)

                # Transform the result
                customer_result = dict_results[0]
                villa_details_list = []
                for r in dict_results:
                    if r["villa_id"] and r["villaName"] and r["community"]:
                        villa_data = {
                            "villaId": r["villa_id"],
                            "villaName": r["villaName"],
                            "community": r["community"],
                            "villaImage": request.build_absolute_uri(settings.MEDIA_URL + r["villaImage"]) if r["villaImage"] else None
                        }
                        villa_details_list.append(villa_data)

                response_data = {
                    "id": customer_result["id"],
                    "customerId": customer_result["customerId"],
                    "customerName": customer_result["customerName"],
                    "villaList": villa_details_list,
                    "emirate": customer_result["emirate"],
                    "contactNumber": customer_result["contactNumber"],
                    "email": customer_result["email"],
                    "status": customer_result["status"],
                    "dateOfBirth": customer_result["dateOfBirth"]
                }
                return success_response(data=response_data, message="Customer fetched successfully")

            # --- FETCH ALL CUSTOMERS ---
            search = request.query_params.get("search", "").strip()
            sort_param = request.query_params.get("sort", "").strip()
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("pageSize", 20))
            is_export = request.query_params.get("isExport", "false").lower() == "true"
            community = request.query_params.get("community", "").strip()
            emirate = request.query_params.get("emirate", "").strip()
            status_filter = request.query_params.get("status", "").strip()

            where_clause = 'WHERE c."isDeleted" = 0'
            params = []

            if search:
                page = 1
                where_clause += """ AND (
                    c."customerId" ILIKE %s OR
                    c."customerName" ILIKE %s OR
                    v."villaName" ILIKE %s OR
                    v.community ILIKE %s OR
                    c.emirate ILIKE %s OR
                    c."contactNumber" ILIKE %s OR
                    c.email ILIKE %s
                )"""
                search_param = f"%{search}%"
                params.extend([search_param] * 7)

            if community:
                where_clause += ' AND v.community ILIKE %s'
                params.append(f"%{community}%")
            
            if emirate:
                where_clause += ' AND c.emirate ILIKE %s'
                params.append(f"%{emirate}%")
            
            if status_filter:
                try:
                    status_value = int(status_filter)
                    where_clause += ' AND c.status = %s'
                    params.append(status_value)
                except ValueError:
                    return error_response(
                        message="Invalid status value. Status must be an integer.",
                        status_code=status.HTTP_400_BAD_REQUEST
                    )

            # Construct sort expression
            sort_expr = 'c.id DESC'
            sort_direction = 'DESC'
            if sort_param:
                parts = sort_param.split(":")
                sort_field = parts[0]
                sort_direction = parts[1].upper() if len(parts) > 1 and parts[1].lower() in ['asc', 'desc'] else 'ASC'
                if sort_field in self.allowed_sort_fields:
                    if sort_field in ["customerName", "emirate"]:
                        sort_expr = f'LOWER(TRIM(c."{sort_field}")) {sort_direction}, c.id {sort_direction}'
                    else:
                        sort_expr = f'c."{sort_field}" {sort_direction}, c.id {sort_direction}'

            order_by = f'ORDER BY {sort_expr}'

            count_query = f"""
                SELECT COUNT(DISTINCT c.id) AS total 
                FROM customer c
                LEFT JOIN "villaDetails" v ON c.id = v."customerId" AND v."isDeleted" = 0
                {where_clause}
            """
            count_params = list(params)
            total_result = execute_query(count_query, count_params, fetch='one')
            
            total_count = 0
            if isinstance(total_result, list) and total_result:
                total_count = total_result[0]["total"] if isinstance(total_result[0], dict) else total_result[0][0]
            elif isinstance(total_result, dict):
                total_count = total_result["total"]
            
            total_pages = (total_count + page_size - 1) // page_size if page_size > 0 else 0

            query = f"""
                SELECT c.id, c."customerId", c."customerName", c.emirate, c."contactNumber", c.email, c.status, c."dateOfBirth",
                       v.id AS villa_id, v."villaName", v.community, v."villaImage"
                FROM customer c
                LEFT JOIN "villaDetails" v ON c.id = v."customerId" AND v."isDeleted" = 0
                {where_clause}
                {order_by}
            """
            query_params = list(params)
            if not is_export:
                query += ' LIMIT %s OFFSET %s'
                query_params.extend([page_size, (page - 1) * page_size])
            
            results = execute_query(query, query_params, many=True)

            columns = [
                "id", "customerId", "customerName", "emirate", "contactNumber", "email", "status", "dateOfBirth",
                "villa_id", "villaName", "community", "villaImage"
            ]
            dict_results = []
            for row in results:
                if isinstance(row, list):
                    dict_results.append(dict(zip(columns, row)))
                else:
                    dict_results.append(row)

            transformed_customers = []
            current_customer_id = None
            current_customer = None

            for result in dict_results:
                if result["id"] != current_customer_id:
                    if current_customer:
                        transformed_customers.append(current_customer)
                    current_customer = {
                        "id": result["id"],
                        "customerId": result["customerId"],
                        "customerName": result["customerName"],
                        "villaList": [],
                        "emirate": result["emirate"],
                        "contactNumber": result["contactNumber"],
                        "email": result["email"],
                        "status": result["status"],
                        "dateOfBirth": result["dateOfBirth"]
                    }
                    current_customer_id = result["id"]

                if result["villa_id"] and result["villaName"] and result["community"]:
                    villa_data = {
                        "villaId": result["villa_id"],
                        "villaName": result["villaName"],
                        "community": result["community"],
                        "villaImage": request.build_absolute_uri(settings.MEDIA_URL + result["villaImage"]) if result["villaImage"] else None
                    }
                    current_customer["villaList"].append(villa_data)

            if current_customer:
                transformed_customers.append(current_customer)

            response_data = {
                "results": transformed_customers,
                "pagination": {
                    "totalRecords": total_count,
                    "totalPages": total_pages,
                    "currentPage": page,
                    "pageSize": page_size,
                },
            }
            return success_response(data=response_data, message="Customers fetched successfully")

        except Exception as e:
            return error_response(message=f"Error fetching customers: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


    def post(self, request):
        """
        Creates a new customer, auto-generates a password, sends it via email, and creates their associated villas with a single image per villa.
        """
        try:
            data = request.POST
            # print(data)

            required_fields = ["customerId", "customerName", "contactNumber"]
            missing_fields = [field for field in required_fields if not data.get(field)]
            if missing_fields:
                return error_response(
                    message=f"Missing required fields: {', '.join(missing_fields)}",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            check_query = 'SELECT id FROM customer WHERE "customerId" = %s'
            check_result = execute_query(check_query, [data["customerId"]], fetch='one')
            if check_result:
                return error_response(
                    message="Customer ID already exists. Please choose a different Customer ID.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            email = data.get("email") or None

            email_check_query = 'SELECT id FROM customer WHERE email = %s AND "isDeleted" = 0'
            email_check_result = execute_query(email_check_query, [email], fetch='one')
            if email_check_result:
                return error_response(
                    message="Email already exists. Please use a different email address.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            contact_query = 'SELECT id FROM customer WHERE "contactNumber" = %s AND "isDeleted" = 0'
            contact_check_result = execute_query(contact_query, [data["contactNumber"]], fetch='one')
            if contact_check_result:
                return error_response(
                    message="Contact number already exists. Please use a different contact number.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )

            raw_password = generate_password()
            encoded_password = encode_password(raw_password)
            date_of_birth = data.get("dateOfBirth") or None

            with transaction.atomic():
                insert_customer_query = """
                    INSERT INTO customer
                    ("customerId", "customerName", emirate, "contactNumber", email, status, password, "isDeleted", "dateOfBirth")
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s)
                    RETURNING id
                """
                params = [
                    data.get("customerId"),
                    data.get("customerName"),
                    data.get("emirate"),
                    data.get("contactNumber"),
                    data.get("email", None),
                    data.get("status", 1),
                    encoded_password,
                    date_of_birth
                ]
                new_customer_result = execute_query(insert_customer_query, params, fetch='one')
                # print(f"DEBUG: new_customer_result = {new_customer_result}")  # Debug logging
                if not new_customer_result:
                    return error_response(
                        message="Failed to create customer",
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )

                # Handle different possible formats of new_customer_result
                if isinstance(new_customer_result, dict):
                    customer_id = new_customer_result.get("id")
                elif isinstance(new_customer_result, (list, tuple)) and len(new_customer_result) > 0:
                    first_item = new_customer_result[0]
                    if isinstance(first_item, dict):
                        customer_id = first_item.get("id")
                    elif isinstance(first_item, (list, tuple)):
                        customer_id = first_item[0]  # Assume first column is id
                    else:
                        customer_id = first_item  # Single value
                else:
                    customer_id = new_customer_result  # Single value
                # print(f"DEBUG: customer_id = {customer_id}")  # Debug logging

                if not customer_id:
                    return error_response(
                        message="Failed to retrieve customer ID from database response",
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                    )

                try:
                    template_name = "New Customer Welcome"  # Replace with your template name
                    context = {
                        "Customer Name": data.get('customerName', ''),
                        "Username": data.get('email', ''),
                        "Password": raw_password
                    }

                    # Check if template exists
                    template_query = 'SELECT subject, body FROM "mailTemplates" WHERE "templateName" = %s AND "isDeleted" = 0 LIMIT 1'
                    template_result = execute_query(template_query, [template_name], fetch='one')

                    if not template_result:
                        # Template not found → use default message
                        print(f"WARNING: Mail template '{template_name}' not found. Using default message.")
                        subject = 'Your New Account Credentials'
                        body = f"""
                        Hello {data.get('customerName')},

                        Welcome! Your customer account has been created.

                        Please use the following credentials for your initial login:
                        Username: {data.get('email')}
                        Password: {raw_password}

                        Thank you for joining us!
                        """
                        send_mail(
                            subject=subject,
                            message=body,
                            from_email=settings.DEFAULT_FROM_EMAIL,
                            recipient_list=[data.get('email')],
                            fail_silently=False
                        )
                    else:
                        # Template exists → use send_mail_with_template
                        send_mail_with_template(
                            template_name=template_name,
                            recipient_email=data.get('email'),
                            context=context
                        )

                except Exception as e:
                    print(f"CRITICAL: Customer {customer_id} created, but failed to send welcome email to {data.get('email')}. Error: {e}")

                # Parse villaList from form data (e.g., villaList[0][villaName])
                villa_details_list = []
                i = 0
                while True:
                    villa_prefix = f'villaList[{i}]'
                    if not any(key.startswith(f'{villa_prefix}[villaName]') for key in data):
                        break
                    villa = {
                        "villaName": data.get(f'{villa_prefix}[villaName]'),
                        "community": data.get(f'{villa_prefix}[community]'),
                        "villaId": data.get(f'{villa_prefix}[villaId]')
                    }
                    if villa["villaName"] and villa["community"]:
                        villa_details_list.append(villa)
                    i += 1

                new_villa_list = []
                for i, villa in enumerate(villa_details_list):
                    if not (villa.get("villaName") and villa.get("community")):
                        continue
                    # Get the single image for this villa
                    villa_image_file = request.FILES.get(f'villaList[{i}][villaImage]')
                    if villa_image_file and not hasattr(villa_image_file, 'name'):
                        return error_response(
                            message=f"Invalid file uploaded for villaList[{i}][villaImage]. Please ensure the file is sent correctly.",
                            status_code=status.HTTP_400_BAD_REQUEST
                        )
                    insert_villa_query = """
                        INSERT INTO "villaDetails"
                        ("customerId", "villaName", community, "isDeleted", "villaImage")
                        VALUES (%s, %s, %s, 0, %s)
                        RETURNING id
                    """
                    # Save image using villa_id (use customer_id temporarily)
                    villa_image_path = save_villa_images(villa_image_file, customer_id)
                    villa_params = [customer_id, villa["villaName"], villa["community"], villa_image_path]
                    result = execute_query(insert_villa_query, villa_params, fetch='one')
                    # print(f"DEBUG: villa insert result = {result}")  # Debug logging
                    if not result:
                        return error_response(
                            message="Failed to create villa",
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )

                    # Handle villa insert result
                    if isinstance(result, dict):
                        villa_id = result.get("id")
                    elif isinstance(result, (list, tuple)) and len(result) > 0:
                        first_item = result[0]
                        if isinstance(first_item, dict):
                            villa_id = first_item.get("id")
                        elif isinstance(first_item, (list, tuple)):
                            villa_id = first_item[0]  # Assume first column is id
                        else:
                            villa_id = first_item  # Single value
                    else:
                        villa_id = result
                    # print(f"DEBUG: villa_id = {villa_id}")  # Debug logging

                    if not villa_id:
                        return error_response(
                            message="Failed to retrieve villa ID from database response",
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )

                    # Update image path with actual villa_id
                    if villa_image_path:
                        new_path = save_villa_images(villa_image_file, villa_id)
                        if new_path != villa_image_path:
                            update_image_query = 'UPDATE "villaDetails" SET "villaImage" = %s WHERE id = %s'
                            execute_query(update_image_query, [new_path, villa_id])
                            villa_image_path = new_path
                    villa["villaId"] = villa_id
                    villa["villaImage"] = request.build_absolute_uri(settings.MEDIA_URL + villa_image_path) if villa_image_path else None
                    new_villa_list.append(villa)

                

                response_data = {
                    "id": customer_id,
                    "customerId": data.get("customerId"),
                    "customerName": data.get("customerName"),
                    "villaList": new_villa_list,
                    "emirate": data.get("emirate"),
                    "contactNumber": data.get("contactNumber"),
                    "email": data.get("email"),
                    "status": data.get("status", 1),
                    "dateOfBirth": data.get("dateOfBirth")
                }

                log_activity_raw(
                    request=request,
                    category='Customer',
                    action='Add',
                    performer=request.user,  # Assuming the view requires authentication
                    details={
                        'id': customer_id,
                        'name': data.get("customerName")
                    }
                )

                return success_response(
                    data=response_data,
                    message="Customer created successfully.",
                    status_code=status.HTTP_201_CREATED
                )

        except (UniqueViolation, IntegrityError):
            return error_response(
                message="Customer ID already exists. Please choose a different Customer ID.",
                status_code=status.HTTP_400_BAD_REQUEST
            )
        except Exception as e:
            return error_response(
                message=f"Error creating customer: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


    def put(self, request, customer_id):
        """
        Updates an existing customer and their associated villas, with a single image per villa.
        """
        try:
            data = request.POST


            # Check if customer exists
            check_query = 'SELECT id FROM customer WHERE id = %s AND "isDeleted" = 0'
            check_result = execute_query(check_query, [customer_id], fetch='one')
            # print(f"DEBUG: check_result = {check_result}")  # Debug logging
            if not check_result:
                return error_response(message="Customer not found", status_code=status.HTTP_404_NOT_FOUND)
            # Handle check_result format
            if isinstance(check_result, dict):
                customer_exists = check_result.get("id") == customer_id
            elif isinstance(check_result, (list, tuple)) and len(check_result) > 0:
                first_item = check_result[0]
                if isinstance(first_item, dict):
                    customer_exists = first_item.get("id") == customer_id
                elif isinstance(first_item, (list, tuple)):
                    customer_exists = first_item[0] == customer_id
                else:
                    customer_exists = first_item == customer_id
            else:
                customer_exists = False
            if not customer_exists:
                return error_response(message="Customer not found", status_code=status.HTTP_404_NOT_FOUND)

            # Check for duplicate email
            email = data.get("email")
            if email:
                email_check_query = 'SELECT id FROM customer WHERE email = %s AND id != %s'
                email_check_result = execute_query(email_check_query, [email, customer_id], fetch='one')
                # print(f"DEBUG: email_check_result = {email_check_result}")  # Debug logging
                if email_check_result:
                    if isinstance(email_check_result, dict):
                        email_exists = email_check_result.get("id") is not None
                    elif isinstance(email_check_result, (list, tuple)) and len(email_check_result) > 0:
                        first_item = email_check_result[0]
                        if isinstance(first_item, dict):
                            email_exists = first_item.get("id") is not None
                        elif isinstance(first_item, (list, tuple)):
                            email_exists = first_item[0] is not None
                        else:
                            email_exists = first_item is not None
                    else:
                        email_exists = False
                    if email_exists:
                        return error_response(
                            message="Email already exists. Please use a different email address.",
                            status_code=status.HTTP_400_BAD_REQUEST
                        )

            # Validate contactNumber length
            contact_number = data.get("contactNumber")
            if contact_number and len(contact_number) > 20:
                return error_response(
                    message="Contact number must be at most 20 characters.",
                    status_code=status.HTTP_400_BAD_REQUEST
                )
            
            # Validate dateOfBirth format (optional)
            date_of_birth = data.get("dateOfBirth")
            if not date_of_birth:
                date_of_birth = None

            with transaction.atomic():
                # Update customer
                update_customer_query = """
                    UPDATE customer
                    SET "customerId" = %s, "customerName" = %s, emirate = %s, "contactNumber" = %s, email = %s, status = %s, "dateOfBirth" = %s
                    WHERE id = %s
                    RETURNING id
                """
                params = [
                    data.get("customerId"),
                    data.get("customerName"),
                    data.get("emirate"),
                    data.get("contactNumber"),
                    data.get("email"),
                    data.get("status", 0),
                    date_of_birth,
                    customer_id,
                    
                ]
                customer_result = execute_query(update_customer_query, params, fetch='one')
                if not customer_result:
                    return error_response(
                        message="Failed to update customer. Possibly due to invalid data (e.g., value too long for field) or customer not found.",
                        status_code=status.HTTP_400_BAD_REQUEST
                    )
                # Handle result to confirm update
                if isinstance(customer_result, dict):
                    updated_customer_id = customer_result.get("id")
                elif isinstance(customer_result, (list, tuple)) and len(customer_result) > 0:
                    first_item = customer_result[0]
                    if isinstance(first_item, dict):
                        updated_customer_id = first_item.get("id")
                    elif isinstance(first_item, (list, tuple)):
                        updated_customer_id = first_item[0] if len(first_item) > 0 else None
                    else:
                        updated_customer_id = first_item
                else:
                    updated_customer_id = None
                if not updated_customer_id:
                    return error_response(
                        message="Failed to update customer. Possibly due to invalid data (e.g., value too long for field) or customer not found.",
                        status_code=status.HTTP_400_BAD_REQUEST
                    )

                # Parse villaList from form data
                villa_details_list = []
                i = 0
                while True:
                    villa_prefix = f'villaList[{i}]'
                    if not any(key.startswith(f'{villa_prefix}[villaName]') for key in data):
                        break
                    villa = {
                        "villaName": data.get(f'{villa_prefix}[villaName]'),
                        "community": data.get(f'{villa_prefix}[community]'),
                        "villaId": data.get(f'{villa_prefix}[villaId]')
                    }
                    if villa["villaName"] and villa["community"]:
                        villa_details_list.append(villa)
                    i += 1

                # Collect existing villa IDs
                provided_existing_ids = []
                for villa in villa_details_list:
                    try:
                        villa_id = int(villa.get("villaId")) if villa.get("villaId") else None
                        if villa_id is not None:
                            provided_existing_ids.append(villa_id)
                    except (ValueError, TypeError):
                        continue

                # Delete villas not in the provided list
                delete_villa_query = 'DELETE FROM "villaDetails" WHERE "customerId" = %s AND "isDeleted" = 0'
                delete_params = [customer_id]
                if provided_existing_ids:
                    delete_villa_query += ' AND id NOT IN %s'
                    delete_params.append(tuple(provided_existing_ids))
                execute_query(delete_villa_query, delete_params)

                new_villa_list = []
                for i, villa in enumerate(villa_details_list):
                    if not (villa.get("villaName") and villa.get("community")):
                        continue
                    try:
                        villa_id = int(villa.get("villaId")) if villa.get("villaId") else None
                    except (ValueError, TypeError):
                        villa_id = None
                    villa_image_file = request.FILES.get(f'villaList[{i}][villaImage]')
                    if villa_image_file and not hasattr(villa_image_file, 'name'):
                        return error_response(
                            message=f"Invalid file uploaded for villaList[{i}][villaImage]. Please ensure the file is sent correctly.",
                            status_code=status.HTTP_400_BAD_REQUEST
                        )
                    
                    villa_image_file = request.FILES.get(f'villaList[{i}][villaImage]')
                    villa_image_param = data.get(f'villaList[{i}][villaImage]')

                    villa_image_path = None
                    if villa_id is not None:
                        # Fetch existing image
                        fetch_images_query = 'SELECT "villaImage" FROM "villaDetails" WHERE id = %s AND "isDeleted" = 0'
                        existing_result = execute_query(fetch_images_query, [villa_id], fetch='one')
                        # print(f"DEBUG: existing_result = {existing_result}")  # Debug logging
                        existing_image = None
                        if existing_result:
                            if isinstance(existing_result, dict):
                                existing_image = existing_result.get("villaImage")
                            elif isinstance(existing_result, (list, tuple)) and len(existing_result) > 0:
                                first_item = existing_result[0]
                                if isinstance(first_item, dict):
                                    existing_image = first_item.get("villaImage")
                                elif isinstance(first_item, (list, tuple)):
                                    existing_image = first_item[0] if len(first_item) > 0 else None
                                else:
                                    existing_image = first_item
                        
                        # Decide what to do with villaImage
                        villa_image_path = existing_image
                        if villa_image_param == "null" or villa_image_param == "":
                            villa_image_path = None  # Explicit clear
                        elif villa_image_file:
                            villa_image_path = save_villa_images(villa_image_file, villa_id)
                        update_villa_query = """
                            UPDATE "villaDetails"
                            SET "villaName" = %s, community = %s, "villaImage" = %s
                            WHERE id = %s AND "customerId" = %s AND "isDeleted" = 0
                            RETURNING id
                        """
                        result = execute_query(update_villa_query, [villa["villaName"], villa["community"], villa_image_path, villa_id, customer_id], fetch='one')
                        # print(f"DEBUG: villa update result = {result}")  # Debug logging
                        if not result:
                            return error_response(message=f"Failed to update villa. Possibly due to invalid data (e.g., value too long for field) or villa not found.", status_code=status.HTTP_400_BAD_REQUEST)
                        # Handle result
                        if isinstance(result, dict):
                            updated_villa_id = result.get("id")
                        elif isinstance(result, (list, tuple)) and len(result) > 0:
                            first_item = result[0]
                            if isinstance(first_item, dict):
                                updated_villa_id = first_item.get("id")
                            elif isinstance(first_item, (list, tuple)):
                                updated_villa_id = first_item[0] if len(first_item) > 0 else None
                            else:
                                updated_villa_id = first_item
                        else:
                            updated_villa_id = None
                        # print(f"DEBUG: updated_villa_id = {updated_villa_id}")  # Debug logging
                        if not updated_villa_id:
                            return error_response(message=f"Failed to update villa. Possibly due to invalid data (e.g., value too long for field) or villa not found.", status_code=status.HTTP_400_BAD_REQUEST)
                        villa_id = updated_villa_id
                    else:
                        # Insert new villa
                        villa_image_path = None
                        if villa_image_param == "null" or villa_image_param == "":
                            villa_image_path = None  # Explicitly null
                        elif villa_image_file:
                            villa_image_path = save_villa_images(villa_image_file, customer_id)
                        insert_villa_query = """
                            INSERT INTO "villaDetails"
                            ("customerId", "villaName", community, "isDeleted", "villaImage")
                            VALUES (%s, %s, %s, 0, %s)
                            RETURNING id
                        """
                        result = execute_query(insert_villa_query, [customer_id, villa["villaName"], villa["community"], villa_image_path], fetch='one')
                        # print(f"DEBUG: villa insert result = {result}")  # Debug logging
                        if not result:
                            return error_response(message="Failed to create villa. Possibly due to invalid data (e.g., value too long for field).", status_code=status.HTTP_400_BAD_REQUEST)
                        # Handle result
                        if isinstance(result, dict):
                            villa_id = result.get("id")
                        elif isinstance(result, (list, tuple)) and len(result) > 0:
                            first_item = result[0]
                            if isinstance(first_item, dict):
                                villa_id = first_item.get("id")
                            elif isinstance(first_item, (list, tuple)):
                                villa_id = first_item[0] if len(first_item) > 0 else None
                            else:
                                villa_id = first_item
                        else:
                            villa_id = None
                        # print(f"DEBUG: villa_id (insert) = {villa_id}")  # Debug logging
                        if not villa_id:
                            return error_response(message="Failed to retrieve villa ID from database response", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
                        # Update image path with actual villa_id
                        if villa_image_path and villa_image_file:
                            new_path = save_villa_images(villa_image_file, villa_id)
                            if new_path != villa_image_path:
                                update_image_query = 'UPDATE "villaDetails" SET "villaImage" = %s WHERE id = %s'
                                execute_query(update_image_query, [new_path, villa_id])
                                villa_image_path = new_path
                    villa["villaId"] = villa_id
                    villa["villaImage"] = request.build_absolute_uri(settings.MEDIA_URL + villa_image_path) if villa_image_path else None
                    new_villa_list.append(villa)
                # print(data)
                response_data = {
                    "id": customer_id,
                    "customerId": data.get("customerId"),
                    "customerName": data.get("customerName"),
                    "villaList": new_villa_list,
                    "emirate": data.get("emirate"),
                    "contactNumber": data.get("contactNumber"),
                    "email": data.get("email"),
                    "status": int(data.get("status", 0)),
                    "dateOfBirth": data.get("dateOfBirth", None)
                }

                log_activity_raw(
                    request=request,
                    category='Customer',
                    action='Update',
                    performer=request.user,  # Assuming the view requires authentication
                    details={
                        'id': customer_id,
                        'name': data.get("customerName")
                    }
                )

                return success_response(data=response_data, message="Customer updated successfully")

        except Exception as e:
            return error_response(message=f"Error updating customer: {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



    
    def delete(self, request, customer_id=None):
        """
        Soft deletes one or multiple customers.
        - If a customer_id is in the URL, it deletes that single customer.
        - If no customer_id is in the URL, it expects a JSON body with a list of customer_ids to delete.
        """
        try:
            ids_to_delete = []
            if customer_id:
                # Handle single delete from URL
                ids_to_delete.append(customer_id)
            else:
                # Handle bulk delete from request body
                data = request.data
                customer_ids_from_body = data.get("customerIds")

                if not customer_ids_from_body or not isinstance(customer_ids_from_body, list):
                    return error_response(message=NO_CUSTOMER_IDS_PROVIDED, status_code=status.HTTP_400_BAD_REQUEST)
                
                if not all(isinstance(i, int) for i in customer_ids_from_body):
                    return error_response(message=ALL_ITEMS_MUST_BE_INTEGERS, status_code=status.HTTP_400_BAD_REQUEST)
                
                ids_to_delete = customer_ids_from_body

            if not ids_to_delete:
                return error_response(message=NO_CUSTOMER_IDS_PROVIDED, status_code=status.HTTP_400_BAD_REQUEST)

            fetch_details_query = 'SELECT id, "customerName" FROM customer WHERE id = ANY(%s) AND "isDeleted" = 0'
            customers_to_delete = execute_query(fetch_details_query, [ids_to_delete], many=True)

            if not customers_to_delete:
                return error_response(message=NO_MATCHING_ACTIVE_CUSTOMERS, status_code=status.HTTP_404_NOT_FOUND)

        # Create a mapping of {id: name} for logging and get a list of IDs that will actually be updated
            name_mapping = {item['id']: item['customerName'] for item in customers_to_delete}
            actual_ids_to_update = list(name_mapping.keys())

            # Use the 'ANY' operator in PostgreSQL for an efficient query with a list of IDs
            # The RETURNING clause gives us back the IDs of the rows that were actually updated.
            query = 'UPDATE customer SET "isDeleted" = 1 WHERE id = ANY(%s) AND "isDeleted" = 0 RETURNING id'
            
            # Note: Your execute_query function should be able to handle a list as a parameter.
            # The psycopg2 library, which Django uses, handles this correctly.
            result = execute_query(query, [ids_to_delete], many=True)


            deleted_count = len(result)

            if deleted_count == 0:
                return error_response(message=NO_MATCHING_ACTIVE_CUSTOMERS, status_code=status.HTTP_404_NOT_FOUND)
            
            deleted_ids = [item['id'] for item in result]
            response_data = {
                "deleted_count": deleted_count,
                "deleted_ids": deleted_ids
            }

            for customer_id, customer_name in name_mapping.items():
                log_activity_raw(
                    request=request,
                    category='Customer',
                    action='Delete',
                    performer=request.user,
                    details={
                        'id': customer_id,
                        'name': customer_name  # This will now have the correct name
                    }
                )

            deleted_count = len(actual_ids_to_update)
            return success_response(message=f"{SUCCESSFULLY_DELETED_CUSTOMERS_PREFIX} {deleted_count} customer(s).")

        except Exception as e:
            return error_response(message=f"Error deleting customer(s): {str(e)}", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)



class CustomerResetPasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def put(self, request, customer_id):
        """
        Allows an admin to reset a customer's password.
        Handles both dict and list return types from execute_query.
        """
        try:
            user = request.user
            print(f"User {user} is attempting to reset password for customer ID {customer_id}")
            # 1. Find the customer using their integer ID
            find_customer_query = """
                SELECT id, "customerName", email FROM customer WHERE id = %s AND "isDeleted" = 0
            """
            query_result = execute_query(find_customer_query, [customer_id], fetch='one')

            if not query_result:
                return error_response(
                    message="Customer not found or has been deleted.",
                    status_code=status.HTTP_404_NOT_FOUND
                )

            # --- SOLUTION: Check the type of the result and get the dictionary ---
            # If execute_query returns a list like [{'id': 8}], get the first element.
            # Otherwise, assume it's already a dictionary like {'id': 8}.
            customer_data = query_result[0] if isinstance(query_result, list) else query_result

            # Now, safely access items using the dictionary `customer_data`
            customer_db_id = customer_data['id']
            customer_name = customer_data['customerName']
            customer_email = customer_data['email']

            # Generate and encode the new password
            new_raw_password = generate_password()
            encoded_password = encode_password(new_raw_password)

            # Define the update query and parameters
            update_query = 'UPDATE customer SET password = %s WHERE id = %s'
            params = [encoded_password, customer_db_id]

            with transaction.atomic():
                # 2. Execute the password update
                execute_query(update_query, params)

                # 3. If the update was successful, send the email
                try:
                    template_name = "Customer Password Reset"
                    context = {
                        "Customer Name": customer_name,
                        "Password": new_raw_password,
                        "Email": customer_email
                    }

                    # Check if template exists in the database
                    template_query = 'SELECT subject, body FROM "mailTemplates" WHERE "templateName" = %s AND COALESCE("isDeleted", 0) = 0 LIMIT 1'
                    template_result = execute_query(template_query, [template_name], fetch='one')

                    if template_result:
                        # Template found → use dynamic send_mail_with_template
                        send_mail_with_template(
                            template_name=template_name,
                            recipient_email=customer_email,
                            context=context
                        )
                    else:
                        # Template not found → fallback to send_mail from settings.py
                        print(f"WARNING: Mail template '{template_name}' not found. Falling back to settings.py email.")
                        subject = 'Your Password Has Been Reset'
                        message = f"""Hello {customer_name},

                        administrator has reset your password. Please use the following temporary password for your next login:

                        Password: {new_raw_password}

                        We recommend you change this password after logging in."""
                        send_mail(
                            subject=subject,
                            message=message,
                            from_email=settings.DEFAULT_FROM_EMAIL,
                            recipient_list=[customer_email],
                            fail_silently=False
                        )
                except Exception as e:
                    print(f"CRITICAL: Email failed for customer {customer_db_id}. Rolling back password change. Error: {e}")
                    raise Exception("Failed to send notification email.") from e

            log_activity_raw(
                request,
                category='Customer',
                action='ResetPassword',
                performer=user,
                details={
                    'id': customer_db_id,
                    'name': customer_name,
                    'email': customer_email

                }
            )

            # 4. Return a success message
            return success_response(
                message="Customer password has been reset successfully. A new password has been sent to their email."
            )

        except TypeError as e:
             # This provides a more specific error message for this exact problem
             print(f"Data access error: {e}. Check the return format of execute_query.")
             return error_response(
                message=f"Error processing data from database: {e}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
             )
        except Exception as e:
            return error_response(
                message=f"An unexpected error occurred: {str(e)}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
