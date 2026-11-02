"""Full session PDF using the same read model as the account details view."""
from services.heavy_operation_service import heavy_operation
import io
import logging
from flask import current_app, jsonify, render_template, send_file


@heavy_operation
def session_pdf_response(account, account_kind):
    from services.session_history_service import iter_session_history
    from services.pdf_export_service import render_parts
    from services.heavy_operation_service import ExportCancelled
    try:
        def parts():
            for rows,total,offset in iter_session_history(account['username']):
                yield render_template('session_history_pdf.html',account=account,
                    account_kind=account_kind,sessions=rows,session_total=total,session_offset=offset)
        payload = render_parts(parts(), current_app.static_folder, timeout=600)
        response = send_file(payload, mimetype='application/pdf',
                             as_attachment=True,
                             download_name=f'{account_kind}-{account["id"]}-sessions.pdf', max_age=0)
        response.call_on_close(payload.close)
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except ExportCancelled:
        return jsonify(success=False,message='أُلغي التصدير ولم يُصدر ملف جزئي.'),409
    except Exception:
        logging.getLogger(__name__).exception('Session PDF export failed')
        return jsonify(success=False, message='تعذر تجهيز ملف الجلسات. أعد المحاولة بعد قليل.'), 503
