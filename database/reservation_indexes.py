"""Bounded, additive maintenance of the reservation release lookup (MariaDB).

Call at a committed schema boundary: ALTER TABLE commits implicitly. This module
does not import application configuration or write reservation/lease data.
"""

TABLE = 'wisp_session_reservations'
INDEX = 'idx_res_radacctid'


def _rows(cur):
    names = [item[0] for item in cur.description]
    return [row if isinstance(row, dict) else dict(zip(names, row))
            for row in cur.fetchall()]


def _state(cur):
    cur.execute(f'SHOW COLUMNS FROM `{TABLE}`')
    columns = {row['Field'].lower() for row in _rows(cur)}
    cur.execute(f'SHOW INDEX FROM `{TABLE}`')
    indexes = _rows(cur)
    usable = any(
        int(row['Seq_in_index']) == 1
        and str(row['Column_name']).lower() == 'radacctid'
        and str(row['Index_type']).upper() == 'BTREE'
        and str(row.get('Ignored', 'NO')).upper() != 'YES'
        and str(row.get('Visible', 'YES')).upper() != 'NO'
        for row in indexes
    )
    return columns, indexes, usable


def ensure_reservation_release_index(conn):
    """Ensure an online, leading radacctid index; accept equivalent indexes.

    Fail on metadata contention instead of falling back to blocking DDL. A
    timeout is a failed migration, safe to retry at the next schema boundary.
    An index-only migration never changes the verified license lease.
    """
    in_transaction = bool(getattr(conn, 'server_status', 0) & 1)
    with conn.cursor() as cur:
        cur.execute('SELECT @@SESSION.lock_wait_timeout AS timeout_value')
        row = cur.fetchone()
        timeout = int(row['timeout_value'] if isinstance(row, dict) else row[0])
        try:
            cur.execute('SET SESSION lock_wait_timeout=2')
            # Bound metadata reads too: a pending/exclusive schema lock can
            # otherwise block SHOW COLUMNS before ALTER is ever attempted.
            columns, indexes, usable = _state(cur)
            if usable:
                return False
            if in_transaction:
                raise RuntimeError('Reservation index DDL requires a committed schema boundary')
            if any(row['Key_name'].lower() == INDEX for row in indexes):
                raise RuntimeError(f'{INDEX} exists but is not a usable radacctid lookup')
            if 'radacctid' not in columns:
                try:
                    cur.execute(f'ALTER TABLE `{TABLE}` ADD COLUMN radacctid BIGINT NULL, '
                                'ALGORITHM=INPLACE, LOCK=NONE')
                except Exception as exc:
                    if not exc.args or exc.args[0] != 1060:
                        raise
                    if 'radacctid' not in _state(cur)[0]:
                        raise
            # Another initializer may have finished while the column was added.
            if not _state(cur)[2]:
                try:
                    cur.execute(f'ALTER TABLE `{TABLE}` ADD INDEX `{INDEX}` (radacctid), '
                                'ALGORITHM=INPLACE, LOCK=NONE')
                except Exception as exc:
                    if not exc.args or exc.args[0] != 1061 or not _state(cur)[2]:
                        raise
            if not _state(cur)[2]:
                raise RuntimeError('Reservation release lookup index was not installed')
        finally:
            cur.execute(f'SET SESSION lock_wait_timeout={timeout}')
    return True
