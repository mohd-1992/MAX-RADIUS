"""Deletion eligibility for static old evidence; never invent a RADIUS Stop."""
import datetime as dt

UNCERTAIN_CLOSES = ('Stale-Session-Timeout','Watchdog-Autoheal-Timeout','Backup-Restored-Closed')
HISTORICAL_GRACE_SECONDS = 900


def historical_observation_allowed(source, session, account):
    if account.get('status') != 'expired' or not account.get('checked_at'):
        return False
    if source == 'radacct' and not session.get('acctstoptime'):
        return False  # A physical open session remains a live blocker at any age.
    values=[]
    try:
        for name in ('acctstarttime','acctupdatetime','acctstoptime'):
            value=session.get(name)
            if value:
                values.append(value if isinstance(value,dt.datetime) else dt.datetime.fromisoformat(str(value)))
        checked=account['checked_at']
        if not isinstance(checked,dt.datetime):checked=dt.datetime.fromisoformat(str(checked))
        return bool(session.get('acctstarttime') and values and
                    max(values) <= checked-dt.timedelta(seconds=HISTORICAL_GRACE_SECONDS))
    except (TypeError,ValueError):
        return False
