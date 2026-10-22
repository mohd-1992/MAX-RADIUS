"""HTTP boundary for the existing modules; underlying services enforce too."""
from flask import request,jsonify,render_template
from core.license_feature_policy import FeatureUnavailable,DEFERRED_ENDPOINTS,endpoint_features,require


def install_feature_access(app):
    @app.errorhandler(FeatureUnavailable)
    def feature_denied(error):
        if request.is_json or request.path.startswith('/api/'):
            return jsonify(success=False,error='FEATURE_NOT_LICENSED',feature=error.feature,message=str(error)),403
        return render_template('access_denied.html'),403

    @app.before_request
    def enforce_features():
        endpoint=(request.endpoint or '').rsplit('.',1)[-1]
        if endpoint in DEFERRED_ENDPOINTS:return None
        value=request.get_json(silent=True)
        data=dict(value) if isinstance(value,dict) else request.values.to_dict()
        if endpoint=='update_settings_action' and any(f.filename for k,f in request.files.items() if k=='network_logo'):
            data['network_logo']='uploaded-logo'
        # Role/state for manager actions comes from the database, not form claims.
        data.pop('_target_role',None);data.pop('_target_active',None)
        if endpoint in ('edit_manager_action','toggle_manager_status_action'):
            identity=(request.view_args or {}).get('manager_id')
            if identity:
                from core.ownership import scoped_query_one
                manager=scoped_query_one('SELECT id,role_id,is_active FROM wisp_managers WHERE id=?',(identity,))
                if manager:data.update(_target_role=manager['role_id'],_target_active=bool(manager['is_active']))
        required=endpoint_features(endpoint,data)
        if required:require(*required)
