# -*- coding: utf-8 -*-
"""
Import Service for MAX RADIUS:
Bulk import of Vouchers and Subscribers from Excel (.xlsx / .xls) files.
Features:
1. Strict Zero-Tolerance Validation (All-or-Nothing).
2. Atomic Database Transactions (Rollback on any failure).
3. Dynamic Sample Excel Template Generation (.xlsx).
4. FreeRADIUS live synchronization (radcheck, radusergroup).
"""

import io
import os
import secrets
import datetime
from database.db import get_connection, is_mysql_conn, query_all, query_one, execute_write, log_audit
from core.rate_limit import format_bytes

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    openpyxl = None

try:
    import xlrd
except ImportError:
    xlrd = None


# -----------------------------------------------------------------------------
# 1. Header Alias Mapping for Flexible Excel Format
# -----------------------------------------------------------------------------
COLUMN_ALIASES = {
    'username': ['username', 'user', 'user_name', 'pin', 'pincode', 'pin_code', 'اسم المستخدم', 'اسم_المستخدم', 'اليوزر', 'رمز الكرت', 'الكرت', 'رقم الكرت', 'الحساب'],
    'password': ['password', 'pass', 'pwd', 'كلمة المرور', 'كلمة_المرور', 'الباسورد', 'الرمز السري', 'رمز المرور'],
    'package': ['package', 'package_name', 'profile', 'plan', 'pkg', 'الباقة', 'اسم الباقة', 'اسم_الباقة', 'البروفايل', 'الخطة'],
    'full_name': ['full_name', 'fullname', 'name', 'subscriber_name', 'الاسم', 'الاسم الكامل', 'اسم المشترك', 'الاسم_الكامل'],
    'phone': ['phone', 'mobile', 'telephone', 'phone_number', 'الهاتف', 'رقم الهاتف', 'الجوال', 'رقم الجوال', 'الموبايل'],
    'service_type': ['service_type', 'type', 'service', 'نوع الخدمة', 'نوع_الخدمة', 'الخدمة', 'نوع الاشتراك'],
    'owner': ['owner', 'reseller', 'distributor', 'agent', 'المالك', 'الموزع', 'نقطة البيع', 'الوكيل', 'اسم الموزع'],
    'mac_binding': ['mac_binding', 'mac', 'mac_address', 'bound_mac', 'الماك', 'عنوان الماك', 'ماك_الجهاز', 'تقييد الماك'],
    'static_ip': ['static_ip', 'ip', 'ip_address', 'الاي بي', 'اي بي ثابت', 'عنوان IP', 'عنوان_IP'],
    'serial_number': ['serial_number', 'serial', 'sn', 's/n', 'الرقم التسلسلي', 'الرقم_التسلسلي', 'سيريال'],
    'notes': ['notes', 'note', 'comment', 'description', 'ملاحظات', 'الملاحظات', 'الوصف']
}

import re

def normalize_header(header_val):
    """Normalize header text to standard field name."""
    if not header_val:
        return ''
    raw_str = str(header_val).strip()
    
    # 1. Check parenthesized hints e.g. (username)* or (package)
    paren_match = re.search(r'\(([^)]+)\)', raw_str)
    if paren_match:
        hint = paren_match.group(1).strip().lower()
        hint_clean = re.sub(r'[^a-z0-9_]', '', hint)
        for std_name in COLUMN_ALIASES.keys():
            if hint_clean == std_name or std_name in hint_clean:
                return std_name

    cleaned = raw_str.lower().replace('_', ' ').replace('-', ' ')
    # Remove asterisks and special chars for matching
    cleaned_clean = re.sub(r'[*()\/:\\]', ' ', cleaned).strip()
    cleaned_no_spaces = re.sub(r'\s+', '', cleaned_clean)
    
    for standard_name, aliases in COLUMN_ALIASES.items():
        if standard_name == cleaned_no_spaces:
            return standard_name
        for alias in aliases:
            a_clean = alias.strip().lower().replace('_', ' ').replace('-', ' ')
            a_no_spaces = re.sub(r'\s+', '', a_clean)
            if cleaned_clean == a_clean or cleaned_no_spaces == a_no_spaces or a_clean in cleaned_clean:
                return standard_name

    return str(header_val).strip()


# -----------------------------------------------------------------------------
# 2. Dynamic Sample Excel Template Generator
# -----------------------------------------------------------------------------
def generate_sample_template(import_type='vouchers'):
    """
    Generates a beautifully formatted sample Excel (.xlsx) file in-memory.
    Includes example rows, instructions, and a reference sheet of available packages.
    """
    if openpyxl is None:
        raise RuntimeError("مكتبة openpyxl غير مثبتة على الخادم.")

    wb = openpyxl.Workbook()
    ws = wb.active

    # Fetch available packages and resellers for reference
    packages = query_all("SELECT id, name, price, service_type, rate_download, rate_upload, volume_quota_mb FROM wisp_packages WHERE is_active = 1 ORDER BY id ASC")
    resellers = query_all("SELECT id, name, phone FROM wisp_resellers WHERE status = 'active' ORDER BY id ASC")

    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
    data_font = Font(name="Segoe UI", size=10)
    center_align = Alignment(horizontal="center", vertical="center")
    right_align = Alignment(horizontal="right", vertical="center")
    
    thin_border = Border(
        left=Side(style='thin', color='CBD5E1'),
        right=Side(style='thin', color='CBD5E1'),
        top=Side(style='thin', color='CBD5E1'),
        bottom=Side(style='thin', color='CBD5E1')
    )

    pkg_sample_name = packages[0]['name'] if packages else "باقة 5 قيقا - 30 يوم"

    if import_type == 'subscribers':
        ws.title = "المشتركون (Subscribers)"
        headers = [
            ("اسم المستخدم (username)*", "username"),
            ("كلمة المرور (password)*", "password"),
            ("اسم الباقة (package)*", "package"),
            ("الاسم الكامل (full_name)", "full_name"),
            ("رقم الهاتف (phone)", "phone"),
            ("نوع الخدمة (service_type)", "service_type"),
            ("الموزع / المالك (owner)", "owner"),
            ("تقييد الماك (mac_binding)", "mac_binding"),
            ("اي بي ثابت (static_ip)", "static_ip"),
            ("ملاحظات (notes)", "notes")
        ]

        sample_rows = [
            ["user_ahmed", "Pass@1234", pkg_sample_name, "أحمد محمد العولقي", "777123456", "pppoe", "المدير العام", "", "", "مشترك منزلي جديد"],
            ["user_salem", "Salem@2026", pkg_sample_name, "سالم صالح الكندي", "771987654", "pppoe", "", "08:00:27:E7:37:40", "", "عميل برج الأمل"],
            ["user_khalid", "Kh#998877", pkg_sample_name, "خالد بن ناصر", "773554433", "hotspot", "", "", "10.10.10.50", "حساب هوتسبوت ثابت"]
        ]
    else:
        ws.title = "الكروت (Vouchers)"
        headers = [
            ("اسم المستخدم / رمز الكرت (username)*", "username"),
            ("كلمة المرور (password)*", "password"),
            ("اسم الباقة (package)*", "package"),
            ("رمز PIN (pin_code)", "pin_code"),
            ("الرقم التسلسلي (serial_number)", "serial_number"),
            ("الموزع / المالك (owner)", "owner"),
            ("ملاحظات (notes)", "notes")
        ]

        sample_rows = [
            ["78451296", "78451296", pkg_sample_name, "78451296", "260900010001", "المدير العام", "كرت هوتسبوت PIN"],
            ["89562314", "99632587", pkg_sample_name, "99632587", "260900010002", "", "كرت يوزر وباسورد"],
            ["45127896", "45127896", pkg_sample_name, "45127896", "260900010003", "", "كرت تجريبي"]
        ]

    # Write Headers
    for col_idx, (display_title, _) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=display_title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center_align
        cell.border = thin_border
    ws.row_dimensions[1].height = 28

    # Write Sample Data Rows
    for row_idx, r_data in enumerate(sample_rows, start=2):
        for col_idx, val in enumerate(r_data, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = data_font
            cell.alignment = center_align if col_idx in [1, 2, 4, 5, 6, 8, 9] else right_align
            cell.border = thin_border
        ws.row_dimensions[row_idx].height = 22

    # Auto-fit column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 5, 18)

    # Add Reference Sheet for Available Packages & Resellers
    ref_ws = wb.create_sheet(title="قائمة الباقات والموزعين")
    ref_ws.views.sheetView[0].rightToLeft = True

    # Section 1: Packages Table
    ref_ws.cell(row=1, column=1, value="قائمة الباقات المعرفة في النظام (انسخ الاسم تماماً كما هو)").font = Font(name="Segoe UI", size=11, bold=True, color="1E3A8A")
    pkg_headers = ["معرف الباقة (ID)", "اسم الباقة (Package Name)", "السعر", "نوع الخدمة", "السرعة", "الكوتا (MB)"]
    for c_idx, ph in enumerate(pkg_headers, start=1):
        c = ref_ws.cell(row=2, column=c_idx, value=ph)
        c.fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")
        c.font = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
        c.alignment = center_align

    for p_idx, p in enumerate(packages, start=3):
        ref_ws.cell(row=p_idx, column=1, value=p['id']).alignment = center_align
        ref_ws.cell(row=p_idx, column=2, value=p['name']).alignment = right_align
        ref_ws.cell(row=p_idx, column=3, value=float(p['price'])).alignment = center_align
        ref_ws.cell(row=p_idx, column=4, value=p['service_type']).alignment = center_align
        ref_ws.cell(row=p_idx, column=5, value=f"{p['rate_download']}/{p['rate_upload']}").alignment = center_align
        ref_ws.cell(row=p_idx, column=6, value=p.get('volume_quota_mb') or 0).alignment = center_align

    # Section 2: Resellers Table
    start_r_row = len(packages) + 5
    ref_ws.cell(row=start_r_row, column=1, value="قائمة الموزعين المعتمدين في النظام").font = Font(name="Segoe UI", size=11, bold=True, color="047857")
    res_headers = ["معرف الموزع (ID)", "اسم الموزع (Reseller Name)", "رقم الهاتف"]
    for c_idx, rh in enumerate(res_headers, start=1):
        c = ref_ws.cell(row=start_r_row + 1, column=c_idx, value=rh)
        c.fill = PatternFill(start_color="10B981", end_color="10B981", fill_type="solid")
        c.font = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
        c.alignment = center_align

    for r_idx, r in enumerate(resellers, start=start_r_row + 2):
        ref_ws.cell(row=r_idx, column=1, value=r['id']).alignment = center_align
        ref_ws.cell(row=r_idx, column=2, value=r['name']).alignment = right_align
        ref_ws.cell(row=r_idx, column=3, value=r.get('phone') or '').alignment = center_align

    for col in ref_ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ref_ws.column_dimensions[col_letter].width = max(max_len + 4, 18)

    ws.views.sheetView[0].rightToLeft = True

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


# -----------------------------------------------------------------------------
# 3. Read Excel Data Rows from File Storage
# -----------------------------------------------------------------------------
def parse_excel_file(file_storage):
    """
    Parses an uploaded file (.xlsx or .xls) and returns a list of dictionaries with raw row numbers.
    """
    filename = file_storage.filename.lower()
    raw_rows = []

    if filename.endswith('.xlsx'):
        if openpyxl is None:
            raise RuntimeError("مكتبة openpyxl غير مثبتة لمعالجة ملفات .xlsx.")
        
        file_bytes = file_storage.read()
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        ws = wb.active
        
        header_map = {}
        for col_idx in range(1, ws.max_column + 1):
            val = ws.cell(row=1, column=col_idx).value
            if val is not None and str(val).strip():
                std_key = normalize_header(val)
                header_map[col_idx] = std_key
                
        if not header_map:
            raise ValueError("الملف فارغ أو لا يحتوي على صف ترويسة صالح في السطر الأول.")

        for row_idx in range(2, ws.max_row + 1):
            row_dict = {}
            has_data = False
            for col_idx, std_key in header_map.items():
                cell_val = ws.cell(row=row_idx, column=col_idx).value
                if cell_val is not None and str(cell_val).strip() != '':
                    has_data = True
                    row_dict[std_key] = str(cell_val).strip()
                else:
                    row_dict[std_key] = ''
            
            if has_data:
                row_dict['_row_number'] = row_idx
                raw_rows.append(row_dict)

    elif filename.endswith('.xls'):
        if xlrd is None:
            raise RuntimeError("مكتبة xlrd غير مثبتة لمعالجة ملفات .xls.")
            
        file_bytes = file_storage.read()
        book = xlrd.open_workbook(file_contents=file_bytes)
        sheet = book.sheet_by_index(0)
        
        if sheet.nrows < 1:
            raise ValueError("الملف فارغ أو لا يحتوي على صف ترويسة.")
            
        header_map = {}
        for col_idx in range(sheet.ncols):
            val = sheet.cell_value(0, col_idx)
            if val is not None and str(val).strip():
                std_key = normalize_header(val)
                header_map[col_idx] = std_key
                
        for row_idx in range(1, sheet.nrows):
            row_dict = {}
            has_data = False
            for col_idx, std_key in header_map.items():
                val = sheet.cell_value(row_idx, col_idx)
                if val is not None and str(val).strip() != '':
                    has_data = True
                    # Format integer numbers from float
                    if isinstance(val, float) and val.is_integer():
                        row_dict[std_key] = str(int(val))
                    else:
                        row_dict[std_key] = str(val).strip()
                else:
                    row_dict[std_key] = ''
            
            if has_data:
                row_dict['_row_number'] = row_idx + 1
                raw_rows.append(row_dict)
    else:
        raise ValueError("صيغة الملف غير مدعومة. يرجى رفع ملف بصيغة (.xlsx أو .xls) فقط.")

    return raw_rows


# -----------------------------------------------------------------------------
# 4. Strict Zero-Tolerance Validation Engine
# -----------------------------------------------------------------------------
def validate_import_data(raw_rows, import_type='vouchers', default_reseller_id=None):
    """
    Performs comprehensive validation on all rows before any database write.
    Rules:
    1. Every row must have non-empty username, password, and package.
    2. Package must exist in wisp_packages table.
    3. No duplicate usernames in the file.
    4. No usernames already existing in database (wisp_subscribers, wisp_vouchers, radcheck).
    5. Returns (is_valid, error_list, cleaned_rows).
    """
    if not raw_rows:
        return False, ["الملف لا يحتوي على أي صفوف بيانات للاستيراد."], []

    # Preload all packages into lookup dict (by exact name, lower name, and ID)
    pkg_rows = query_all("SELECT * FROM wisp_packages")
    pkgs_by_name = {}
    pkgs_by_id = {}
    for p in pkg_rows:
        pkgs_by_name[p['name'].strip().lower()] = p
        pkgs_by_id[p['id']] = p

    # Preload all resellers
    reseller_rows = query_all("SELECT * FROM wisp_resellers")
    resellers_by_name = {}
    resellers_by_id = {}
    for r in reseller_rows:
        resellers_by_name[r['name'].strip().lower()] = r
        resellers_by_id[r['id']] = r

    # Preload all existing usernames from DB into sets for O(1) duplicate checks
    existing_radcheck_users = set()
    for row in query_all("SELECT LOWER(username) as u FROM radcheck"):
        existing_radcheck_users.add(row['u'])

    existing_sub_users = set()
    for row in query_all("SELECT LOWER(username) as u FROM wisp_subscribers"):
        existing_sub_users.add(row['u'])

    existing_voucher_users = set()
    for row in query_all("SELECT LOWER(username) as u FROM wisp_vouchers"):
        existing_voucher_users.add(row['u'])

    errors = []
    seen_file_usernames = {}  # lower_user -> first_row_number
    cleaned_rows = []

    for r in raw_rows:
        row_num = r.get('_row_number', '?')
        u = r.get('username', '').strip()
        p = r.get('password', '').strip()
        pkg_name_input = r.get('package', '').strip()

        # 1. Mandatory Fields Check
        if not u:
            errors.append(f"الصف رقم {row_num}: حقل 'اسم المستخدم (username)' فارغ.")
            continue

        if not p:
            if import_type == 'vouchers':
                # For vouchers, if password is empty, PIN-only mode uses username as password
                p = u
            else:
                errors.append(f"الصف رقم {row_num}: حقل 'كلمة المرور (password)' فارغ للمستخدم [{u}].")
                continue

        if not pkg_name_input:
            errors.append(f"الصف رقم {row_num}: حقل 'اسم الباقة (package)' فارغ للمستخدم [{u}].")
            continue

        # 2. Package Validation
        matched_pkg = None
        pkg_key = pkg_name_input.lower()
        if pkg_key in pkgs_by_name:
            matched_pkg = pkgs_by_name[pkg_key]
        elif pkg_name_input.isdigit() and int(pkg_name_input) in pkgs_by_id:
            matched_pkg = pkgs_by_id[int(pkg_name_input)]

        if not matched_pkg:
            errors.append(f"الصف رقم {row_num}: اسم الباقة [{pkg_name_input}] غير موجود في النظام للمستخدم [{u}]. يرجى مراجعة قائمة الباقات المعرفة.")
            continue

        # 3. File Internal Duplicate Check
        u_lower = u.lower()
        if u_lower in seen_file_usernames:
            first_row = seen_file_usernames[u_lower]
            errors.append(f"الصف رقم {row_num}: اسم المستخدم [{u}] مكرر داخل نفس ملف الإكسل (موجود مسبقاً في الصف رقم {first_row}).")
            continue
        seen_file_usernames[u_lower] = row_num

        # 4. Database Conflict Check (FreeRADIUS / Subscribers / Vouchers)
        if u_lower in existing_radcheck_users or u_lower in existing_sub_users or u_lower in existing_voucher_users:
            errors.append(f"الصف رقم {row_num}: اسم المستخدم [{u}] مسجل مسبقاً في قاعدة بيانات النظام و FreeRADIUS.")
            continue

        # 5. Reseller / Owner Determination
        owner_input = r.get('owner', '').strip()
        matched_reseller_id = default_reseller_id

        if owner_input:
            o_key = owner_input.lower()
            if o_key in ['admin', 'المدير العام', 'المدير', 'مدير', 'superadmin', '0', 'none']:
                matched_reseller_id = None
            elif o_key in resellers_by_name:
                matched_reseller_id = resellers_by_name[o_key]['id']
            elif owner_input.isdigit() and int(owner_input) in resellers_by_id:
                matched_reseller_id = int(owner_input)

        cleaned_item = {
            'row_num': row_num,
            'username': u,
            'password': p,
            'pin_code': r.get('pin_code', '').strip() or u,
            'serial_number': r.get('serial_number', '').strip() or f"{datetime.datetime.now().strftime('%y%m')}{secrets.randbelow(100000000):08d}",
            'package_id': matched_pkg['id'],
            'package_name': matched_pkg['name'],
            'package_price': float(matched_pkg['price']),
            'package_cost': float(matched_pkg.get('cost') or 0.0),
            'reseller_id': matched_reseller_id,
            'full_name': r.get('full_name', '').strip() or u,
            'phone': r.get('phone', '').strip(),
            'service_type': r.get('service_type', '').strip().lower() or matched_pkg.get('service_type', 'pppoe'),
            'mac_binding': r.get('mac_binding', '').strip(),
            'static_ip': r.get('static_ip', '').strip(),
            'notes': r.get('notes', '').strip()
        }
        cleaned_rows.append(cleaned_item)

    if errors:
        return False, errors, []

    return True, [], cleaned_rows


# -----------------------------------------------------------------------------
# 5. Atomic Batch Import Execution
# -----------------------------------------------------------------------------
def execute_import(import_type, cleaned_rows, batch_name=None, admin_username='admin'):
    """
    Executes the entire database insertion inside an atomic transaction.
    Rolls back completely if any exception occurs.
    """
    if not cleaned_rows:
        return False, "لا توجد بيانات صالحة للإدخال.", None

    total_count = len(cleaned_rows)
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    conn = get_connection()
    is_mysql = is_mysql_conn(conn)
    
    try:
        cursor = conn.cursor()
        if is_mysql:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0;")
        else:
            cursor.execute("PRAGMA foreign_keys = OFF;")

        if import_type == 'vouchers':
            # 1. Create Voucher Batch
            if not batch_name or not batch_name.strip():
                batch_name = f"استيراد إكسل ({total_count} كرت) - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}"

            first_pkg_id = cleaned_rows[0]['package_id']
            first_reseller_id = cleaned_rows[0]['reseller_id']
            batch_num = f"IMP{datetime.datetime.now().strftime('%y%m%d%H%M%S')}-{secrets.randbelow(1000):03d}"

            if is_mysql:
                cursor.execute('''
                    INSERT INTO wisp_voucher_batches (
                        batch_number, name, package_id, reseller_id, card_count,
                        prefix, pin_only, char_type, code_length, created_by
                    ) VALUES (%s, %s, %s, %s, %s, '', 0, 'mixed', 8, %s)
                ''', (batch_num, batch_name, first_pkg_id, first_reseller_id, total_count, admin_username))
                batch_id = cursor.lastrowid
            else:
                cursor.execute('''
                    INSERT INTO wisp_voucher_batches (
                        batch_number, name, package_id, reseller_id, card_count,
                        prefix, pin_only, char_type, code_length, created_by
                    ) VALUES (?, ?, ?, ?, ?, '', 0, 'mixed', 8, ?)
                ''', (batch_num, batch_name, first_pkg_id, first_reseller_id, total_count, admin_username))
                batch_id = cursor.lastrowid

            # 2. Insert Vouchers, radcheck, and radusergroup
            for c in cleaned_rows:
                ph = "%s" if is_mysql else "?"
                cursor.execute(f'''
                    INSERT INTO wisp_vouchers (
                        batch_id, package_id, reseller_id, serial_number, username, password, pin_code, status, bound_mac
                    ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'unused', {ph})
                ''', (
                    batch_id, c['package_id'], c['reseller_id'], c['serial_number'],
                    c['username'], c['password'], c['pin_code'], c.get('mac_binding', '')
                ))

                # Insert FreeRADIUS check & usergroup
                cursor.execute(f'''
                    INSERT INTO radcheck (username, attribute, op, value)
                    VALUES ({ph}, 'Cleartext-Password', ':=', {ph})
                ''', (c['username'], c['password']))

                cursor.execute(f'''
                    INSERT INTO radusergroup (username, groupname, priority)
                    VALUES ({ph}, {ph}, 1)
                ''', (c['username'], c['package_name']))

                if c.get('mac_binding') and len(c['mac_binding']) > 5:
                    cursor.execute(f'''
                        INSERT INTO radcheck (username, attribute, op, value)
                        VALUES ({ph}, 'Calling-Station-Id', '==', {ph})
                    ''', (c['username'], c['mac_binding'].upper()))

            if is_mysql:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
            conn.commit()

            log_audit(1, admin_username, 'IMPORT_VOUCHERS', 'vouchers',
                      f'Successfully imported {total_count} vouchers into batch "{batch_name}" ({batch_num})')

            return True, f"تم بنجاح استيراد {total_count} كرت وتوليد الحزمة [{batch_name}] ومزامنتها مع FreeRADIUS.", {
                'count': total_count,
                'batch_id': batch_id,
                'batch_number': batch_num,
                'batch_name': batch_name
            }

        else:
            # ----------------- Subscribers Import -----------------
            ph = "%s" if is_mysql else "?"
            created_sub_ids = []

            for s in cleaned_rows:
                cursor.execute(f'''
                    INSERT INTO wisp_subscribers (
                        username, password, full_name, phone, address,
                        service_type, package_id, mac_binding, static_ip, status, notes, created_at
                    ) VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, 'active', {ph}, {ph})
                ''', (
                    s['username'], s['password'], s['full_name'], s['phone'], '',
                    s['service_type'], s['package_id'], s['mac_binding'], s['static_ip'],
                    s['notes'], now_str
                ))
                sub_id = cursor.lastrowid
                created_sub_ids.append(sub_id)

                # FreeRADIUS entries
                cursor.execute(f'''
                    INSERT INTO radcheck (username, attribute, op, value)
                    VALUES ({ph}, 'Cleartext-Password', ':=', {ph})
                ''', (s['username'], s['password']))

                cursor.execute(f'''
                    INSERT INTO radusergroup (username, groupname, priority)
                    VALUES ({ph}, {ph}, 1)
                ''', (s['username'], s['package_name']))

                if s.get('mac_binding') and len(s['mac_binding']) > 5:
                    cursor.execute(f'''
                        INSERT INTO radcheck (username, attribute, op, value)
                        VALUES ({ph}, 'Calling-Station-Id', '==', {ph})
                    ''', (s['username'], s['mac_binding'].upper()))

                if s.get('static_ip') and len(s['static_ip']) > 6:
                    cursor.execute(f'''
                        INSERT INTO radreply (username, attribute, op, value)
                        VALUES ({ph}, 'Framed-IP-Address', ':=', {ph})
                    ''', (s['username'], s['static_ip']))

            if is_mysql:
                cursor.execute("SET FOREIGN_KEY_CHECKS = 1;")
            conn.commit()

            log_audit(1, admin_username, 'IMPORT_SUBSCRIBERS', 'subscribers',
                      f'Successfully imported {total_count} subscribers from Excel.')

            return True, f"تم بنجاح استيراد {total_count} مشترك ثابت وتفعيل حساباتهم ومزامنتها مع FreeRADIUS.", {
                'count': total_count,
                'sub_ids': created_sub_ids
            }

    except Exception as e:
        conn.rollback()
        return False, f"فشل الاستيراد أثناء الكتابة في قاعدة البيانات (تم التراجع الكامل Rollback): {str(e)}", None
    finally:
        conn.close()
