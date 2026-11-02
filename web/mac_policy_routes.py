"""Shared response handling; blueprint endpoints retain their own permission names."""
from flask import request, jsonify, current_app
from core.rbac import get_current_manager, has_permission
from core.ownership import scoped_query_one


def account_mac_response(kind, account_id):
    from services.account_mac_service import get_mac_policy, save_mac_policy, TABLES
    from services.renewal_settlement_service import SettlementPending
    permission = 'subscribers.edit' if kind == 'subscriber' else 'detail.vouchers.card_edit'
    manager = get_current_manager()
    if not manager:
        return jsonify(success=False, message='يرجى تسجيل الدخول أولًا.'), 401
    if not has_permission(permission, manager) or (kind == 'voucher' and not has_permission('vouchers.actions', manager)):
        return jsonify(success=False, message='لا تملك صلاحية تعديل هذا الحساب.'), 403
    if not scoped_query_one(f'SELECT id FROM {TABLES[kind]} WHERE id=?', (account_id,)):
        return jsonify(success=False, message='الحساب غير موجود.'), 404
    try:
        if request.method == 'GET':
            return jsonify(success=True, policy=get_mac_policy(kind, account_id))
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('بيانات قائمة الماكات غير صالحة.')
        return jsonify(save_mac_policy(kind, account_id, data, actor=manager.get('username') or 'admin'))
    except PermissionError:
        return jsonify(success=False, message='لا تملك صلاحية الوصول إلى هذا الحساب.'), 403
    except ValueError as exc:
        return jsonify(success=False, message=str(exc)), 400
    except SettlementPending as exc:
        return jsonify(success=False, message=str(exc)), 409
    except Exception:
        current_app.logger.exception('MAC policy operation incomplete: kind=%s id=%s', kind, account_id)
        return jsonify(success=False, message='تعذر تأكيد النتيجة. راجع قائمة الماكات قبل إعادة الحفظ.'), 500
