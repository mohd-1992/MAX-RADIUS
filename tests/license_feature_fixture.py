"""Explicit licensed identity for SQL/ownership fixtures, without runtime imports.

Entitlement denial is tested separately by test_license_feature_policy.py and
the signed-package Docker fixture; these existing tests isolate roles/money.
"""
from core.license_feature_policy import enabled,endpoint_features


def fixture_endpoint_enabled(endpoint,data=None):
    return all(enabled(key,{'valid':True,'features':{}})
               for key in endpoint_features(endpoint,data))
