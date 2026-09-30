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
        "border_radius": 0,
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
            "prefix": "",
            "text": "السعر: 5",
            "x": 82, "y": 10,
            "font_family": "Tajawal", "font_size": 10, "color": "#059669",
            "bold": True, "italic": False, "underline": False, "align": "left",
            "letter_spacing": 0
        },
        "username": {
            "enabled": True,
            "prefix": "",
            "sample_value": "88492015",
            "x": 50, "y": 42,
            "font_family": "Tajawal", "font_size": 15, "color": "#1e293b",
            "bold": True, "italic": False, "underline": False, "align": "center",
            "letter_spacing": 1
        },
        "password": {
            "enabled": True,
            "prefix": "",
            "sample_value": "88492015",
            "x": 50, "y": 58,
            "font_family": "Tajawal", "font_size": 15, "color": "#1e293b",
            "bold": True, "italic": False, "underline": False, "align": "center",
            "letter_spacing": 1
        },
        "pin_code": {
            "enabled": False,
            "prefix": "",
            "sample_value": "4920-8815",
            "x": 50, "y": 50,
            "font_family": "Tajawal", "font_size": 16, "color": "#1d4ed8",
            "bold": True, "italic": False, "underline": False, "align": "center",
            "letter_spacing": 2
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

        # Resolve bg_image to absolute static URL if it is a filename
        bg_val = d.get('bg_image') or ''
        if bg_val and not bg_val.startswith('/') and not bg_val.startswith('http'):
            d['bg_image'] = f"/static/uploads/card_backgrounds/{bg_val}"
        if d.get('config') and isinstance(d['config'], dict):
            if 'general' in d['config'] and isinstance(d['config']['general'], dict):
                d['config']['general']['border_radius'] = 0
                if d['bg_image']:
                    d['config']['general']['bg_image_url'] = d['bg_image']

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

    # Resolve bg_image to absolute static URL if it is a filename
    bg_val = d.get('bg_image') or ''
    if bg_val:
        clean_filename = os.path.basename(bg_val)
        d['bg_image_filename'] = clean_filename
        d['bg_image'] = f"/static/uploads/card_backgrounds/{clean_filename}"
    else:
        d['bg_image_filename'] = ''
        d['bg_image'] = ''

    if d.get('config') and isinstance(d['config'], dict):
        if 'general' in d['config'] and isinstance(d['config']['general'], dict):
            d['config']['general']['border_radius'] = 0
            if d['bg_image']:
                d['config']['general']['bg_image'] = d.get('bg_image_filename', '')
                d['config']['general']['bg_image_url'] = d['bg_image']

    cols = d.get('cards_per_row') or 2
    rows_cnt = d.get('cards_per_col') or 5
    d['cards_per_page'] = cols * rows_cnt
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
    def _parse_val(v, default, vtype=float):
        if v is not None and str(v).strip() != '':
            try:
                return vtype(v)
            except Exception:
                pass
        return default

    width_mm = round(_parse_val(data.get('width_mm') or card_dims.get('width_mm'), 85.0, float), 1)
    height_mm = round(_parse_val(data.get('height_mm') or card_dims.get('height_mm'), 52.0, float), 1)
    cards_per_row = int(_parse_val(data.get('cards_per_row') or page_layout.get('cards_per_row'), 2, int))
    cards_per_col = int(_parse_val(data.get('cards_per_col') or page_layout.get('cards_per_col'), 5, int))
    margin_top_mm = int(_parse_val(data.get('margin_top_mm') or page_layout.get('margin_top_mm'), 10, int))
    margin_page_mm = int(_parse_val(data.get('margin_page_mm') or page_layout.get('margin_page_mm'), 10, int))
    card_spacing_mm = int(_parse_val(data.get('card_spacing_mm') if data.get('card_spacing_mm') is not None else page_layout.get('card_spacing_mm'), 0, int))
    cut_lines = 1 if data.get('cut_lines') in [1, '1', True, 'true', 'on'] else 0
    bg_color = data.get('background_color', '#ffffff')
    border_color = data.get('border_color', '#cbd5e1')
    bg_image = data.get('bg_image', '').strip()
    if bg_image:
        bg_image = os.path.basename(bg_image)
    elif design_id and int(design_id) > 0 and data.get('design_type') != 'clean':
        # If bg_image not explicitly passed in payload, inspect config or retain existing from DB
        cfg_bg = (config_obj.get('general', {}) if isinstance(config_obj, dict) else {}).get('bg_image')
        if cfg_bg:
            bg_image = os.path.basename(cfg_bg)
        else:
            old_row = query_one("SELECT bg_image FROM wisp_card_templates WHERE id = ?", (int(design_id),))
            if old_row and old_row.get('bg_image'):
                bg_image = old_row.get('bg_image')

    is_default = 1 if data.get('is_default') in [1, '1', True, 'true', 'on'] else 0

    config_obj = data.get('config')
    if isinstance(config_obj, dict):
        if 'general' in config_obj and isinstance(config_obj['general'], dict):
            config_obj['general']['bg_image'] = bg_image
            config_obj['general']['bg_image_url'] = f"/static/uploads/card_backgrounds/{bg_image}" if bg_image else ''
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

    try:
        from PIL import Image, ImageOps

        file_storage.seek(0)
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img)

        orig_w, orig_h = img.size
        natural_ratio = float(orig_w) / float(orig_h)

        orig_fname = getattr(file_storage, 'filename', '') or 'card_bg.png'
        ext = os.path.splitext(orig_fname)[1].lower()
        if ext not in ['.png', '.jpg', '.jpeg', '.webp']:
            ext = '.png'

        filename = f"card_bg_{uuid.uuid4().hex[:12]}{ext}"
        save_path = os.path.join(upload_folder, filename)

        # Save original file bytes directly to preserve 100% genuine colors, color profiles (sRGB/ICC), and sharpness
        file_storage.seek(0)
        file_storage.save(save_path)

        w_mm = float(target_width_mm) if target_width_mm else 85.0
        suggested_h_mm = round(w_mm / natural_ratio, 1)

        return {
            'success': True,
            'filename': filename,
            'url': f'/static/uploads/card_backgrounds/{filename}',
            'file_url': f'/static/uploads/card_backgrounds/{filename}',
            'width': orig_w,
            'height': orig_h,
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
