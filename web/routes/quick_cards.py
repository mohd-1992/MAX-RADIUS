"""Small issuance UI; all money and cards are created by the batch service."""
import secrets
import logging
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, abort
from core.rbac import get_current_manager
from core.ownership import owner_id, scoped_query_all, require_owned
from services.quick_card_service import (get_issue_policy, save_issue_policy, issue_quick_cards,
                                        quick_issue_history, get_quick_issue, record_quick_print)
from services.voucher_service import get_vouchers
from services.card_design_service import get_all_card_designs

quick_cards_bp = Blueprint('quick_cards_bp', __name__)
logger = logging.getLogger(__name__)


def page_context(show_form=True):
    from web.access_policy import can_access_endpoint
    manager = get_current_manager()
    packages = scoped_query_all("SELECT id,name,price,cost FROM wisp_packages WHERE is_active=1 AND service_type IN ('hotspot','both') ORDER BY id")
    history, total = [], 0
    page = max(1, int(request.args.get('page', 1)))
    if can_access_endpoint('quick_cards_history'):
        history, total = quick_issue_history(request.args.get('q','').strip(), request.args.get('package_id'),
            request.args.get('from',''), request.args.get('to',''), request.args.get('reseller_id'), page)
    resellers = scoped_query_all("SELECT id,COALESCE(NULLIF(full_name,''),username) name,wallet_balance,allowed_packages FROM wisp_managers WHERE is_active=1 AND COALESCE(is_deleted,0)=0 ORDER BY id") if owner_id() is None else []
    return dict(show_form=show_form,packages=packages,history=history,total=total,page=page,
        resellers=resellers,manager=manager,policy=get_issue_policy(),request_key=secrets.token_urlsafe(24),
        designs=get_all_card_designs(),can_history=can_access_endpoint('quick_cards_history'))


@quick_cards_bp.route('/vouchers/quick', endpoint='quick_cards_page')
def quick_cards_page():
    try:
        return render_template('quick_cards.html', **page_context())
    except ValueError as exc:
        flash(str(exc),'danger')
        return render_template('quick_cards.html', show_form=False, history=[],total=0,page=1,
                               packages=[],resellers=[],can_history=False)


@quick_cards_bp.route('/vouchers/quick/history', endpoint='quick_cards_history')
def quick_cards_history():
    try:
        return render_template('quick_cards.html', **page_context(False))
    except ValueError:
        abort(400)


@quick_cards_bp.route('/vouchers/quick/issue', methods=['POST'], endpoint='quick_cards_issue')
def quick_cards_issue():
    data = request.get_json(silent=True) if request.is_json else request.form
    if not hasattr(data, 'get'):
        return jsonify(success=False,message='صيغة الطلب غير صالحة.'),400
    try:
        result = issue_quick_cards(get_current_manager(), data.get('package_id'),data.get('count'),
                                  data.get('request_key'),data.get('reseller_id') or None)
        return jsonify(success=True,issue_id=result['id'],repeated=result['repeated'],
                       amount=str(result['amount']),balance_after=str(result['balance_after']) if result['balance_after'] is not None else None,
                       message='تم استرجاع الإصدار السابق دون خصم جديد.' if result['repeated'] else 'تم إصدار الكروت بنجاح.')
    except (ValueError,PermissionError) as exc:
        return jsonify(success=False,message=str(exc)),400
    except Exception:
        logger.exception('Quick issuance failed')
        return jsonify(success=False,message='تعذر إكمال الإصدار؛ احتفظ بمرجع الطلب وأعد المحاولة بنفسه للتحقق من النتيجة.'),500


@quick_cards_bp.route('/vouchers/quick/<int:issue_id>', endpoint='quick_cards_details')
def quick_cards_details(issue_id):
    issue = get_quick_issue(issue_id)
    if not issue:
        abort(404)
    require_owned('wisp_voucher_batches', ident=issue['batch_id'])
    return render_template('quick_card_details.html', issue=issue,
        cards=get_vouchers(batch_id=issue['batch_id'],limit=50), designs=get_all_card_designs())


@quick_cards_bp.route('/vouchers/quick/<int:issue_id>/print', methods=['POST'], endpoint='quick_cards_print')
def quick_cards_print(issue_id):
    issue=get_quick_issue(issue_id)
    if not issue:
        abort(404)
    require_owned('wisp_voucher_batches',ident=issue['batch_id'])
    if not get_vouchers(batch_id=issue['batch_id'],limit=1):
        flash('كروت هذا الإصدار حُذفت؛ لا يمكن إعادة طباعتها.','danger')
        return redirect(url_for('quick_cards_history'))
    from web.routes.vouchers import print_cards
    # Existing print renderer, guarded here by the dedicated quick-print permission.
    # Count opened print views, never claim proof that a physical printer ran.
    response=print_cards(issue['batch_id'])
    record_quick_print(issue_id)
    return response


@quick_cards_bp.route('/vouchers/reseller-issue-settings', methods=['GET','POST'], endpoint='reseller_issue_settings')
def reseller_issue_settings():
    if request.method=='POST':
        try:
            save_issue_policy(request.form)
            flash('حُفظت إعدادات كروت الموزعين؛ ستطبق على الكروت الجديدة فقط.','success')
            return redirect(url_for('reseller_issue_settings'))
        except ValueError as exc:
            flash(str(exc),'danger')
    return render_template('reseller_issue_settings.html',policy=get_issue_policy())
