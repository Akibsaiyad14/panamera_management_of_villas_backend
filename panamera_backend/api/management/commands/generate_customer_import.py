import os
import pandas as pd
import secrets
import string
import base64
from django.core.management.base import BaseCommand
from django.db import connection, transaction

class Command(BaseCommand):
    help = 'Import Customers and Villas from Excel (No Emails)'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Full path to the Excel file')

    # --- HELPER: Generate Random Password ---
    def generate_password(self, length=3):
        first_char = secrets.choice(string.ascii_uppercase)
        next_two_chars = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(2))
        number_part = ''.join(secrets.choice(string.digits) for _ in range(length))
        return first_char + next_two_chars + number_part

    # --- HELPER: Encode Password (Base64) ---
    def encode_password(self, password):
        encoded_bytes = base64.b64encode(password.encode("utf-8"))
        return encoded_bytes.decode("utf-8")

    def handle(self, *args, **options):
        file_path = options['file_path']

        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f"File not found: {file_path}"))
            return

        self.stdout.write(f"Reading file: {file_path}")

        try:
            df = pd.read_excel(file_path)
            df = df.fillna('')
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error reading Excel: {e}"))
            return

        customers_created = 0
        villas_created = 0

        with connection.cursor() as cursor:
            for index, row in df.iterrows():
                try:
                    def get_val(col_name):
                        return str(row.get(col_name, '')).strip()

                    # Corrected column names
                    customer_ref_id = get_val('CUSTOMER ID')
                    client_name = get_val('Names')
                    villa_name = get_val('Villa')
                    community = get_val('Community')
                    emirate = get_val('Emirate')
                    email = get_val('MAIL')
                    mobile = get_val('CONTACTS')

                    if email == 'nan' or not email:
                        email = None

                    # Excel float fix
                    if mobile.endswith('.0'):
                        mobile = mobile[:-2]

                    # Phone formatting
                    if mobile:
                        if mobile.startswith('0'):
                            mobile = mobile[1:]
                        if not mobile.startswith('+971'):
                            mobile = f"+971 {mobile}"

                    # Skip invalid rows
                    if not customer_ref_id or not client_name:
                        self.stdout.write(self.style.WARNING(f"Row {index}: Missing ID or Name. Skipping."))
                        continue

                    # --- DATABASE LOGIC ---
                    with transaction.atomic():
                        # Check if customer exists
                        cursor.execute('SELECT id FROM customer WHERE "customerId" = %s', [customer_ref_id])
                        existing_cust = cursor.fetchone()

                        # Check phone ownership
                        cursor.execute(
                            'SELECT id, "customerId" FROM customer WHERE "contactNumber" = %s',
                            [mobile]
                        )
                        existing_contact = cursor.fetchone()

                        if existing_contact:
                            existing_contact_pk, existing_contact_ref = existing_contact

                            # Different customer? Reject
                            if existing_contact_ref != customer_ref_id:
                                self.stdout.write(
                                    self.style.WARNING(
                                        f"Row {index}: Contact number {mobile} belongs to another customer ({existing_contact_ref}). Skipping."
                                    )
                                )
                                continue

                        customer_pk = None

                        if existing_cust:
                            # Reuse existing customer
                            customer_pk = existing_cust[0]
                            self.stdout.write(f"  Customer {customer_ref_id} ({client_name}) already exists. Checking Villa...")

                        else:
                            # New customer creation
                            if existing_contact:
                                # Phone belongs to same customerId -> reuse
                                customer_pk = existing_contact_pk
                            else:
                                # Create new customer
                                raw_pw = self.generate_password()
                                encoded_pw = self.encode_password(raw_pw)

                                insert_query = """
                                    INSERT INTO customer
                                    ("customerId", "customerName", emirate, "contactNumber", email, status, password, "isDeleted", "dateOfBirth")
                                    VALUES (%s, %s, %s, %s, %s, 1, %s, 0, NULL)
                                    RETURNING id
                                """
                                params = [customer_ref_id, client_name, emirate, mobile, email, encoded_pw]

                                cursor.execute(insert_query, params)
                                customer_pk = cursor.fetchone()[0]
                                customers_created += 1

                                self.stdout.write(self.style.SUCCESS(
                                    f"  Created Customer: {client_name} | Phone: {mobile} | PW: {raw_pw}"
                                ))

                        # --- CREATE VILLA ---
                        if villa_name and community:
                            cursor.execute(
                                'SELECT id FROM "villaDetails" WHERE "customerId" = %s AND "villaName" = %s',
                                [customer_pk, villa_name]
                            )

                            if not cursor.fetchone():
                                cursor.execute(
                                    """
                                    INSERT INTO "villaDetails"
                                    ("customerId", "villaName", community, "isDeleted", "villaImage")
                                    VALUES (%s, %s, %s, 0, NULL)
                                    """,
                                    [customer_pk, villa_name, community]
                                )
                                villas_created += 1
                                self.stdout.write(f"    -> Added Villa: {villa_name}")

                except Exception as row_e:
                    self.stdout.write(self.style.ERROR(f"Error on Row {index}: {row_e}"))

        # --- FINAL SUMMARY ---
        self.stdout.write(self.style.SUCCESS(
            f"\nImport Finished: {customers_created} Customers Created, {villas_created} Villas Added."
        ))
