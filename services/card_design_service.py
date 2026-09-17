# -*- coding: utf-8 -*-
"""
Card Design and Voucher Template Service:
Manages multi-design voucher templates, layout calculations, typography,
and automated image processing (resize/crop) for print-ready voucher cards.
"""

import os
import json
import uuid
import datetime
from database.db import query_all, query_one, execute_write
from core.config import DB_TYPE

# Default base configuration for voucher cards
DEFAULT_CARD_CONFIG = {
    "version": 2,
    "general": {
        "name": "التصميم الافتراضي الحديث",
        "design_type": "image",
        "width_mm": 85,
        "height_mm": 52,
        "bg_color": "#ffffff",
        "border_color": "#cbd5e1",
        "border_radius": 8,
        "border_width": 1,
        "bg_image": "",
        "bg_opacity": 1.0
    },
    "layout": {
        "cards_per_row": 2,
        "cards_per_col": 5,
        "margin_top_mm": 10,
        "margin_page_mm": 10,
        "spacing_mm": 3,
        "cut_lines": True,
        "cut_line_style": "dashed",
        "cut_line_color": "#94a3b8"
    },
    "elements": {
        "network_name": {
            "enabled": True,
            "text": "شبكة الأفق الذكية",
            "x": 50, "y": 8,
            "font_family": "Tajawal", "font_size": 13, "color": "#0f172a",
            "bold": True, "italic": False, "underline": False, "align": "center"
        },
        "package_name": {
            "enabled": True,
            "text": "باقة 5 جيجا - أسبوعية",
            "x": 50, "y": 18,
            "font_family": "Tajawal", "font_size": 10, "color": "#2563eb",
            "bold": True, "italic": False, "underline": False, "align": "center"
        },
        "price": {
            "enabled": True,
            "text": "السعر: 5 ر.س",
            "x": 82, "y": 10,
            "font_family": "Tajawal", "font_size": 10, "color": "#059669",
            "bold": True, "italic": False, "underline": False, "align": "left"
        },
        "username": {
            "enabled": True,
            "prefix": "المستخدم: ",
            "sample_value": "88492015",
            "x": 50, "y": 42,
            "font_family": "Tajawal", "font_size": 15, "color": "#1e293b",
            "bold": True, "italic": False, "underline": False, "align": "center"
        },
        "password": {
            "enabled": True,
            "prefix": "الرمز: ",
            "sample_value": "88492015",
            "x": 50, "y": 58,
            "font_family": "Tajawal", "font_size": 15, "color": "#1e293b",
            "bold": True, "italic": False, "underline": False, "align": "center"
        },
        "pin_code": {
            "enabled": False,
            "prefix": "رمز الكرت: ",
            "sample_value": "4920-8815",
            "x": 50, "y": 50,
            "font_family": "Tajawal", "font_size": 16, "color": "#1d4ed8",
            "bold": True, "italic": False, "underline": False, "align": "center"
        },
        "serial": {
            "enabled": True,
            "prefix": "S/N: ",
            "sample_value": "V26-10492",
            "x": 15, "y": 88,
            "font_family": "Tajawal", "font_size": 8, "color": "#64748b",
            "bold": False, "italic": False, "underline": False, "align": "right"
        },
        "expiry": {
            "enabled": True,
            "prefix": "الصلاحية: ",
            "sample_value": "30 يوم",
            "x": 85, "y": 88,
            "font_family": "Tajawal", "font_size": 8, "color": "#64748b",
            "bold": False, "italic": False, "underline": False, "align": "left"
        },
        "qr_code": {
            "enabled": True,
            "x": 82, "y": 50,
            "size": 60,
            "sample_url": "http://wifi.hotspot/login?user=88492015"
        },
        "custom_text": {
            "enabled": True,
            "text": "امسح رمز الـ QR أو سجل الدخول بالمتصفح",
            "x": 50, "y": 92,
            "font_family": "Tajawal", "font_size": 7.5, "color": "#94a3b8",
            "bold": False, "italic": False, "underline": False, "align": "center"
        }
    }
}

_schema_checked = False

def ensure_card_designs_table():
    """Ensure wisp_card_templates table has all modern columns."""
    global _schema_checked
    if _schema_checked:
        return
    try:
        if DB_TYPE == 'mysql':
            cols_info = query_all("SHOW COLUMNS FROM wisp_card_templates")
            existing_cols = [c.get('Field') or c.get('COLUMN_NAME') or c.get('name') for c in (cols_info or [])]
        else:
            cols_info = query_all("PRAGMA table_info(wisp_card_templates)")
            existing_cols = [c.get('name') for c in (cols_info or [])]

        new_columns = [
            ("design_type", "VARCHAR(30) DEFAULT 'image'"),
            ("cards_per_col", "INTEGER DEFAULT 5"),
            ("margin_top_mm", "INTEGER DEFAULT 10"),
            ("margin_page_mm", "INTEGER DEFAULT 10"),
            ("card_spacing_mm", "INTEGER DEFAULT 3"),
            ("cut_lines", "INTEGER DEFAULT 1"),
            ("bg_image", "VARCHAR(255) DEFAULT ''"),
            ("config_json", "TEXT"),
            ("created_at", "VARCHAR(30)"),
            ("updated_at", "VARCHAR(30)")
        ]

        for col_name, col_def in new_columns:
            if col_name not in existing_cols:
                try:
                    execute_write(f"ALTER TABLE wisp_card_templates ADD COLUMN {col_name} {col_def}")
                except Exception:
                    pass
        
        count_row = query_one("SELECT COUNT(*) as cnt FROM wisp_card_templates")
        if not count_row or count_row.get('cnt', 0) == 0:
            now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            execute_write("""
                INSERT INTO wisp_card_templates (
                    name, design_type, width_mm, height_mm, cards_per_row, cards_per_col,
                    margin_top_mm, margin_page_mm, card_spacing_mm, cut_lines,
                    background_color, border_color, is_default, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """, (
                'التصميم الافتراضي للشبكة', 'image', 85, 52, 2, 5,
                10, 10, 3, 1,
                '#ffffff', '#cbd5e1', json.dumps(DEFAULT_CARD_CONFIG, ensure_ascii=False),
                now_str, now_str
            ))
        _schema_checked = True
    except Exception as e:
        print(f"ensure_card_designs_table error: {e}")


def get_all_card_designs():
    """Retrieve all saved card design templates."""
    ensure_card_designs_table()
    rows = query_all("SELECT * FROM wisp_card_templates ORDER BY is_default DESC, id DESC")
    designs = []
    for r in (rows or []):
        d = dict(r)
        if d.get('config_json'):
            try:
                d['config'] = json.loads(d['config_json'])
            except Exception:
                d['config'] = DEFAULT_CARD_CONFIG
        else:
            d['config'] = DEFAULT_CARD_CONFIG

        cols = d.get('cards_per_row') or 2
        rows_cnt = d.get('cards_per_col') or 5
        d['cards_per_page'] = cols * rows_cnt
        d['created_at_display'] = d.get('created_at') or '2026-01-01'
        designs.append(d)
    return designs


def get_card_design_by_id(design_id):
    """Retrieve a single card design template by ID."""
    ensure_card_designs_table()
    row = query_one("SELECT * FROM wisp_card_templates WHERE id = ?", (design_id,))
    if not row:
        return None
    d = dict(row)
    if d.get('config_json'):
        try:
            d['config'] = json.loads(d['config_json'])
        except Exception:
            d['config'] = DEFAULT_CARD_CONFIG
    else:
        d['config'] = DEFAULT_CARD_CONFIG
    return d


def get_default_card_design():
    """Get the active default card design."""
    ensure_card_designs_table()
    row = query_one("SELECT * FROM wisp_card_templates WHERE is_default = 1 LIMIT 1")
    if not row:
        row = query_one("SELECT * FROM wisp_card_templates ORDER BY id ASC LIMIT 1")
    if row:
        return get_card_design_by_id(row['id'])
    return {
        'id': 1,
        'name': 'التصميم الافتراضي',
        'design_type': 'image',
        'width_mm': 85,
        'height_mm': 52,
        'cards_per_row': 2,
        'cards_per_col': 5,
        'margin_top_mm': 10,
        'margin_page_mm': 10,
        'card_spacing_mm': 3,
        'cut_lines': 1,
        'background_color': '#ffffff',
        'border_color': '#cbd5e1',
        'bg_image': '',
        'config': DEFAULT_CARD_CONFIG,
        'is_default': 1
    }


def save_card_design(data, upload_dir=None):
    """
    Create or update a card design.
    data: dict containing design parameters and full config_json.
    """
    ensure_card_designs_table()
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    design_id = data.get('id') or data.get('design_id') or data.get('template_id')
    card_dims = data.get('card_dimensions') or {}
    page_layout = data.get('page_layout') or {}
    
    name = str(data.get('name', 'تصميم جديد')).strip() or 'تصميم جديد'
    design_type = data.get('design_type', 'image')
    width_mm = int(data.get('width_mm') or card_dims.get('width_mm') or 85)
    height_mm = int(data.get('height_mm') or card_dims.get('height_mm') or 52)
    cards_per_row = int(data.get('cards_per_row') or page_layout.get('cards_per_row') or 2)
    cards_per_col = int(data.get('cards_per_col') or page_layout.get('cards_per_col') or 5)
    margin_top_mm = int(data.get('margin_top_mm') or page_layout.get('margin_top_mm') or 10)
    margin_page_mm = int(data.get('margin_page_mm') or page_layout.get('margin_page_mm') or 10)
    card_spacing_mm = int(data.get('card_spacing_mm') or page_layout.get('spacing_horizontal_mm') or page_layout.get('card_spacing_mm') or 3)
    cut_lines = 1 if data.get('cut_lines') in [1, '1', True, 'true', 'on'] else 0
    bg_color = data.get('background_color', '#ffffff')
    border_color = data.get('border_color', '#cbd5e1')
    bg_image = data.get('bg_image', '').strip()
    is_default = 1 if data.get('is_default') in [1, '1', True, 'true', 'on'] else 0

    config_obj = data.get('config')
    if isinstance(config_obj, dict):
        config_json_str = json.dumps(config_obj, ensure_ascii=False)
    elif data.get('config_json'):
        config_json_str = str(data.get('config_json'))
    else:
        config_json_str = json.dumps(DEFAULT_CARD_CONFIG, ensure_ascii=False)

    if is_default == 1:
        execute_write("UPDATE wisp_card_templates SET is_default = 0")

    if design_id and int(design_id) > 0:
        design_id = int(design_id)
        execute_write("""
            UPDATE wisp_card_templates SET
                name = ?, design_type = ?, width_mm = ?, height_mm = ?,
                cards_per_row = ?, cards_per_col = ?, margin_top_mm = ?, margin_page_mm = ?,
                card_spacing_mm = ?, cut_lines = ?, background_color = ?, border_color = ?,
                bg_image = ?, config_json = ?, is_default = ?, updated_at = ?
            WHERE id = ?
        """, (
            name, design_type, width_mm, height_mm,
            cards_per_row, cards_per_col, margin_top_mm, margin_page_mm,
            card_spacing_mm, cut_lines, bg_color, border_color,
            bg_image, config_json_str, is_default, now_str, design_id
        ))
        saved_id = design_id
    else:
        saved_id = execute_write("""
            INSERT INTO wisp_card_templates (
                name, design_type, width_mm, height_mm, cards_per_row, cards_per_col,
                margin_top_mm, margin_page_mm, card_spacing_mm, cut_lines,
                background_color, border_color, bg_image, config_json, is_default,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name, design_type, width_mm, height_mm,
            cards_per_row, cards_per_col, margin_top_mm, margin_page_mm,
            card_spacing_mm, cut_lines, bg_color, border_color,
            bg_image, config_json_str, is_default, now_str, now_str
        ))

    if is_default == 1 and config_json_str:
        existing_setting = query_one("SELECT `key` FROM wisp_system_settings WHERE `key` = 'voucher_custom_design_config'")
        if existing_setting:
            execute_write("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = 'voucher_custom_design_config'", (config_json_str,))
        else:
            execute_write("INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES ('voucher_custom_design_config', ?, 'تخصيصات قالب الكروت والطباعة')", (config_json_str,))

    return {'success': True, 'design_id': saved_id, 'message': 'تم حفظ التصميم بنجاح'}


def delete_card_design(design_id):
    """Delete a card design by ID, ensuring at least one default remains."""
    ensure_card_designs_table()
    design = get_card_design_by_id(design_id)
    if not design:
        return False, "التصميم المطلوب غير موجود."
    
    total = query_one("SELECT COUNT(*) as cnt FROM wisp_card_templates")
    if total and total.get('cnt', 0) <= 1:
        return False, "لا يمكن حذف التصميم الوحيد المتبقي في النظام."

    was_default = design.get('is_default') == 1
    execute_write("DELETE FROM wisp_card_templates WHERE id = ?", (design_id,))

    if was_default:
        execute_write("UPDATE wisp_card_templates SET is_default = 1 WHERE id = (SELECT id FROM wisp_card_templates ORDER BY id ASC LIMIT 1)")

    return True, "تم حذف التصميم بنجاح."


def set_default_card_design(design_id):
    """Set a design as the system default for voucher printing."""
    ensure_card_designs_table()
    design = get_card_design_by_id(design_id)
    if not design:
        return False, "التصميم المطلوب غير موجود."
    
    execute_write("UPDATE wisp_card_templates SET is_default = 0")
    execute_write("UPDATE wisp_card_templates SET is_default = 1 WHERE id = ?", (design_id,))

    if design.get('config_json'):
        existing = query_one("SELECT `key` FROM wisp_system_settings WHERE `key` = 'voucher_custom_design_config'")
        if existing:
            execute_write("UPDATE wisp_system_settings SET `value` = ? WHERE `key` = 'voucher_custom_design_config'", (design['config_json'],))
        else:
            execute_write("INSERT INTO wisp_system_settings (`key`, `value`, `description`) VALUES ('voucher_custom_design_config', ?, 'تخصيصات قالب الكروت والطباعة')", (design['config_json'],))

    return True, f"تم تعيين [{design['name']}] كقالب افتراضي لطباعة الكروت."


def duplicate_card_design(design_id):
    """Duplicate an existing design to allow rapid variations."""
    ensure_card_designs_table()
    original = get_card_design_by_id(design_id)
    if not original:
        return None, "التصميم المطلوب غير موجود."

    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    new_name = f"{original['name']} (نسخة مكررة)"
    
    new_id = execute_write("""
        INSERT INTO wisp_card_templates (
            name, design_type, width_mm, height_mm, cards_per_row, cards_per_col,
            margin_top_mm, margin_page_mm, card_spacing_mm, cut_lines,
            background_color, border_color, bg_image, config_json, is_default,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
    """, (
        new_name, original.get('design_type', 'image'),
        original.get('width_mm', 85), original.get('height_mm', 52),
        original.get('cards_per_row', 2), original.get('cards_per_col', 5),
        original.get('margin_top_mm', 10), original.get('margin_page_mm', 10),
        original.get('card_spacing_mm', 3), original.get('cut_lines', 1),
        original.get('background_color', '#ffffff'), original.get('border_color', '#cbd5e1'),
        original.get('bg_image', ''), original.get('config_json', ''),
        now_str, now_str
    ))

    return new_id, f"تم تكرار التصميم بنجاح باسم [{new_name}]."


def process_and_save_card_background(file_storage, target_width_mm=85, target_height_mm=52, upload_folder=None):
    """
    Automated Image Processing (Pillow):
    1. Opens uploaded image and corrects EXIF orientation.
    2. Calculates natural aspect ratio without destructive cropping.
    3. Resizes to high-resolution print-ready dimensions maintaining 100% of the image.
    4. Computes proportional card dimensions (e.g. 85mm x H_mm) so the card matches the image perfectly.
    5. Saves optimized file into static/uploads/card_backgrounds.
    """
    if not upload_folder:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        upload_folder = os.path.join(base_dir, 'web', 'static', 'uploads', 'card_backgrounds')
    
    os.makedirs(upload_folder, exist_ok=True)

    filename = f"card_bg_{uuid.uuid4().hex[:12]}.png"
    save_path = os.path.join(upload_folder, filename)

    try:
        from PIL import Image, ImageOps

        file_storage.seek(0)
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img)

        orig_w, orig_h = img.size
        natural_ratio = float(orig_w) / float(orig_h)

        # High resolution print width (e.g. 1200px)
        print_target_w = 1200
        print_target_h = int(round(print_target_w / natural_ratio))

        # High quality smooth resize without any cropping
        img_resized = img.resize((print_target_w, print_target_h), Image.Resampling.LANCZOS)

        if img_resized.mode not in ('RGB', 'RGBA'):
            img_resized = img_resized.convert('RGBA')

        img_resized.save(save_path, 'PNG', optimize=True)

        w_mm = float(target_width_mm) if target_width_mm else 85.0
        suggested_h_mm = round(w_mm / natural_ratio, 1)

        return {
            'success': True,
            'filename': filename,
            'url': f'/static/uploads/card_backgrounds/{filename}',
            'file_url': f'/static/uploads/card_backgrounds/{filename}',
            'width': print_target_w,
            'height': print_target_h,
            'aspect_ratio': round(natural_ratio, 3),
            'orig_width': orig_w,
            'orig_height': orig_h,
            'suggested_width_mm': w_mm,
            'suggested_height_mm': suggested_h_mm
        }

    except Exception as e:
        file_storage.seek(0)
        file_storage.save(save_path)
        w_mm = float(target_width_mm) if target_width_mm else 85.0
        h_mm = float(target_height_mm) if target_height_mm else 52.0
        return {
            'success': True,
            'filename': filename,
            'url': f'/static/uploads/card_backgrounds/{filename}',
            'file_url': f'/static/uploads/card_backgrounds/{filename}',
            'width': 1000,
            'height': int(1000 * (h_mm / w_mm)),
            'aspect_ratio': round(w_mm / h_mm, 3),
            'suggested_width_mm': w_mm,
            'suggested_height_mm': h_mm,
            'warning': f'Saved without Pillow processing: {str(e)}'
        }

init_card_templates_table = ensure_card_designs_table
