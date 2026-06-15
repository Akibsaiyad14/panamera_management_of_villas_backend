import os
import json
import re
import datetime
import pandas as pd
from django.core.management.base import BaseCommand
from django.db import connection, transaction

# Import your utils
from api.utils import (
    create_or_update_amc_schedules,
    generate_jobs_and_tasks_for_amc,
    AMC_STATUS_ACTIVE,
    AMC_STATUS_INACTIVE
)

class Command(BaseCommand):
    help = 'Import AMC data from Excel using Raw SQL with Auto-Header Detection and Optimized Job Generation'

    def add_arguments(self, parser):
        parser.add_argument('file_path', type=str, help='Full path to the Excel file')

    def handle(self, *args, **options):
        file_path = options['file_path']

        # --- 1. FILE VALIDATION ---
        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f"File not found at: {file_path}"))
            return

        self.stdout.write(f"Reading file: {file_path}")

        try:
            # --- 2. HEADER AUTO-DETECTION (Scan all sheets) ---
            xls = pd.ExcelFile(file_path)
            sheet_names = xls.sheet_names
            
            target_df = None
            found_sheet = None

            for sheet in sheet_names:
                # Read first 50 rows without header to find "Job No"
                df_raw = pd.read_excel(xls, sheet_name=sheet, header=None)
                
                for i, row in df_raw.head(50).iterrows():
                    # Convert row to lowercase string list
                    row_str_values = [str(val).strip().lower() for val in row.values]
                    
                    if 'job no' in row_str_values:
                        self.stdout.write(self.style.SUCCESS(f"Found headers in sheet '{sheet}' at Row {i+1}"))
                        # Reload sheet starting from this row as header
                        target_df = pd.read_excel(xls, sheet_name=sheet, header=i)
                        target_df = target_df.fillna('')
                        found_sheet = sheet
                        break
                if target_df is not None:
                    break

            if target_df is None:
                self.stdout.write(self.style.ERROR("CRITICAL: Could not find column 'Job No' in any sheet."))
                return

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error reading Excel file: {e}"))
            return

        # --- 3. SCOPE MAPPING CONFIGURATION ---
        # Maps Excel keywords -> Database "amcMaintenanceTasks.type"
        SCOPE_MAPPING = {
            "garden": "Garden Maintenance",
            "swp": "Pool Maintenance",     
            "pool": "Pool Maintenance",
            "water": "Water Feature",      
            "feature": "Water Feature"
        }

        self.stdout.write("Starting Import...")

        with connection.cursor() as cursor:
            count = 0
            for index, row in target_df.iterrows():
                try:
                    # Helper to find value by column name (insensitive)
                    def get_val(target_col):
                        target = target_col.lower().strip()
                        for col in target_df.columns:
                            if str(col).lower().strip() == target:
                                return str(row[col]).strip()
                        return ''

                    # --- A. PARSE JOB ID ---
                    job_id = get_val('Job No')
                    # Skip empty rows or repeated headers
                    if not job_id or job_id.lower() == 'job no' or job_id.lower() == 'nan':
                        continue

                    # Check if already exists
                    cursor.execute('SELECT 1 FROM "AMCMaster" WHERE "jobId" = %s', [job_id])
                    if cursor.fetchone():
                        self.stdout.write(self.style.WARNING(f"Skipping {job_id} - Already exists"))
                        continue

                    # --- B. PARSE CUSTOMER ---
                    customer_name = get_val('Client Name as per System')
                    raw_contact = get_val('Contact Number')
                    if raw_contact.endswith('.0'): raw_contact = raw_contact[:-2]
                    customer_id = raw_contact

                    # --- C. PARSE SCOPE ---
                    raw_scope_text = get_val('AMC TYPE').lower()
                    scope_list = []
                    for key, db_value in SCOPE_MAPPING.items():
                        if key in raw_scope_text and db_value not in scope_list:
                            scope_list.append(db_value)

                    # --- D. PARSE VISIT DAYS ---
                    raw_days = get_val('Vist days') # Handle Excel typo
                    if not raw_days: raw_days = get_val('Visit days')

                    day_map = {'mon': 'Monday', 'tue': 'Tuesday', 'wed': 'Wednesday', 'thu': 'Thursday', 'fri': 'Friday', 'sat': 'Saturday', 'sun': 'Sunday'}
                    found_days = set()
                    matches = re.findall(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)', raw_days, re.IGNORECASE)
                    for match in matches:
                        found_days.add(day_map[match[:3].lower()])
                    visit_days_list = list(found_days)

                    # --- E. PARSE LOCATION (VILLA) ---
                    location_str = get_val('Location')
                    villa_id = None
                    
                    if location_str:
                        # 1. Try matching Villa Name
                        cursor.execute('SELECT id FROM "villaDetails" WHERE "villaName" ILIKE %s LIMIT 1', [f"%{location_str}%"])
                        res = cursor.fetchone()
                        
                        # 2. Try matching Community
                        if not res:
                             cursor.execute('SELECT id FROM "villaDetails" WHERE "community" ILIKE %s LIMIT 1', [f"%{location_str}%"])
                             res = cursor.fetchone()

                        if res:
                            villa_id = res[0]
                        else:
                            # Warn but do not fail
                            self.stdout.write(self.style.WARNING(f"  Warning: Location '{location_str}' not matched. Setting VillaID to NULL."))

                    # --- F. PARSE START DATE ---
                    start_date = None
                    # Find date column dynamically
                    date_col_name = None
                    for c in target_df.columns:
                        if 'contrct' in str(c).lower() or 'renewal' in str(c).lower():
                            date_col_name = c
                            break
                    
                    if date_col_name:
                        raw_date_val = row[date_col_name]
                        if isinstance(raw_date_val, (datetime.datetime, pd.Timestamp)):
                            start_date = raw_date_val.date()
                        else:
                            d_str = str(raw_date_val).strip()
                            for fmt in ('%d-%b-%y', '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
                                try:
                                    start_date = datetime.datetime.strptime(d_str, fmt).date()
                                    break
                                except: pass

                    if not start_date:
                        self.stdout.write(self.style.ERROR(f"Skipping {job_id}: Invalid Date"))
                        continue

                    # --- G. INSERT & GENERATE ---
                    with transaction.atomic():
                        # 1. Insert Master Record
                        insert_query = """
                            INSERT INTO "AMCMaster"
                            ("jobId", "amcJobName", "customerId", "startDate", "duration", 
                             "visitDays", "scopeOfWork", "villaId", "status", "isDeleted", "createdAt")
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, NOW())
                            RETURNING "amcId"
                        """
                        params = [
                            job_id,
                            f"{customer_name} - {raw_scope_text}",
                            customer_id,
                            start_date,
                            12,
                            json.dumps(visit_days_list),
                            json.dumps(scope_list),
                            villa_id,
                            AMC_STATUS_ACTIVE
                        ]

                        cursor.execute(insert_query, params)
                        new_amc_id = cursor.fetchone()[0]

                        # 2. Create Schedule Mappings (Junction Table)
                        create_or_update_amc_schedules(new_amc_id, scope_list, visit_days_list)
                        
                        # 3. OPTIMIZED JOB GENERATION
                        today = datetime.date.today()
                        
                        # If start_date is in the past (e.g. July), begin generating visits from TODAY.
                        # If start_date is in the future, begin generating from start_date.
                        if start_date < today:
                            gen_start = today
                        else:
                            gen_start = start_date

                        # Only generate for the next 45 days to save server load.
                        gen_end = gen_start + datetime.timedelta(days=45)

                        jobs, tasks = generate_jobs_and_tasks_for_amc(new_amc_id, gen_start, gen_end)

                        self.stdout.write(self.style.SUCCESS(
                            f"Imported {job_id} -> Generated {jobs} jobs (Range: {gen_start} to {gen_end})"
                        ))
                        count += 1

                except Exception as row_e:
                    self.stdout.write(self.style.ERROR(f"Error on row {index}: {row_e}"))

        self.stdout.write(f"--- Finished. Imported {count} AMCs from sheet '{found_sheet}'. ---")
