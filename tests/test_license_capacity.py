"""Run only against the disposable max-license-test-db container."""
import os, json, hashlib, hmac, unittest, socket, struct, secrets, concurrent.futures, datetime
import pymysql
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
import base64
from database.schema_healer import heal_database_schema
from services import license_guard_service as guard
from core import licensing


def connect():
    if os.environ.get('DB_HOST') != 'max-license-test-db':
        raise RuntimeError('This suite requires the disposable test database')
    c=pymysql.connect(host='max-license-test-db',user='radius',password='radpass',database='radius_wisp',autocommit=True,charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor)
    with c.cursor() as q:q.execute("SET time_zone='+03:00'")
    return c


def packet(code,user,auth=None,attrs=None,password='pass'):
    auth=auth or secrets.token_bytes(16)
    attributes=[(1,user.encode()),(4,socket.inet_aton('10.1.0.1'))]+(attrs or [])
    if code==1:
        pwd=password.encode().ljust(16,b'\0'); mask=hashlib.md5(b'max123'+auth).digest()
        attributes.append((2,bytes(a^b for a,b in zip(pwd,mask))))
    body=b''.join(bytes((t,len(v)+2))+v for t,v in attributes)
    if code==1:body+=bytes((80,18))+bytes(16)
    head=struct.pack('!BBH',code,17,20+len(body))
    if code==4:auth=hashlib.md5(head+b'\0'*16+body+b'max123').digest()
    if code==1:body=body[:-16]+hmac.new(b'max123',head+auth+body,hashlib.md5).digest()
    raw=head+auth+body
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.settimeout(4);sock.sendto(raw,(os.environ.get('RADIUS_TEST_HOST','max-license-test-core'),1812 if code==1 else 1813));reply=sock.recv(4096)
    assert reply[4:20]==hashlib.md5(reply[:4]+auth+reply[20:]+b'max123').digest()
    return reply

class CapacityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Enforce the disposable-target guard before any schema operation.
        c=connect()
        assert heal_database_schema(backfill=False)
        from database.subscriber_edit_schema import install_subscriber_edit_schema
        install_subscriber_edit_schema(c)
        with c.cursor() as q:
            q.execute("INSERT INTO nas(nasname,shortname,type,secret) VALUES('0.0.0.0/0','capacity-tests','other','max123')")
        c.close()
    def setUp(self):
        self.c=connect();self.q=self.c.cursor()
        for t in ['radacct','radpostauth','wisp_session_reservations','wisp_revoked_licenses','wisp_license_info']:
            self.q.execute('DELETE FROM '+t)
        self.q.execute("INSERT INTO wisp_license_info(license_id,client_name,hardware_id,raw_package_json,signature_b64,status,max_active_sessions) VALUES('fixture','fixture','ANY','{}','test','active',3)")
        self.q.execute("UPDATE wisp_license_runtime_state SET license_row_id=LAST_INSERT_ID(),package_hash=SHA2('{}',256),is_valid=1,license_mode='active_sessions',max_active_sessions=3,valid_until=UTC_TIMESTAMP()+INTERVAL 10 MINUTE WHERE id=1")
        self.q.execute("DELETE FROM radcheck WHERE username LIKE 'capacity-%' OR username IN ('ping','testbuyer')")
        for user in ['capacity-a','capacity-b','capacity-c','capacity-d','ping','testbuyer']:
            self.q.execute("INSERT INTO radcheck(username,attribute,op,value) VALUES(%s,'Cleartext-Password',':=','pass')",(user,))
    def tearDown(self):self.q.close();self.c.close()
    def reserve(self,n,user='capacity-a',station='AA',nas='10.1.0.1'):
        c=connect()
        with c.cursor() as q:
            q.execute("SELECT fn_reserve_license_slot(%s,%s,%s,%s,'1','10.0.0.2') AS n",(f'{n:032x}',user,nas,station));r=q.fetchone()['n']
        c.close();return r
    def test_atomic_and_retry(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:r=list(pool.map(self.reserve,range(1,25)))
        self.assertEqual(r.count(1),3,r)
        winner=next(i for i,x in enumerate(r,1) if x==1)
        self.assertEqual(self.reserve(winner),1)
    def test_independent_same_account(self):
        self.assertEqual([self.reserve(i) for i in range(1,5)],[1,1,1,0])
    def test_no_mac_still_reserved(self):
        self.assertEqual([self.reserve(i,station='') for i in range(1,5)],[1,1,1,0])
    def test_link_and_stop_only_one(self):
        for i in [1,2,3]:self.assertEqual(self.reserve(i),1)
        self.q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,callingstationid,acctstarttime,acctupdatetime,class) VALUES('s','s','capacity-a','10.1.0.1','AA',NOW(),NOW(),%s)",('maxcap:'+f'{1:032x}',))
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations WHERE radacctid IS NULL');self.assertEqual(self.q.fetchone()['n'],2)
        self.assertEqual(self.reserve(4),0)
        self.q.execute("UPDATE radacct SET acctinputoctets=12345,acctoutputoctets=6789 WHERE acctuniqueid='s'")
        self.q.execute("SELECT acctinputoctets,acctoutputoctets FROM radacct WHERE acctuniqueid='s'");self.assertEqual(tuple(self.q.fetchone().values()),(12345,6789))
        self.q.execute("UPDATE radacct SET acctstoptime=NOW() WHERE acctuniqueid='s'")
        self.assertEqual(self.reserve(4),1)
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations WHERE radacctid IS NULL');self.assertEqual(self.q.fetchone()['n'],3)
    def test_revoked_lease_package_changed(self):
        self.q.execute("UPDATE wisp_license_info SET status='revoked'");self.assertEqual(self.reserve(1),-1)
        self.q.execute("UPDATE wisp_license_info SET status='active',raw_package_json='{\"changed\":true}'");self.assertEqual(self.reserve(1),-1)
    def test_stale_verification_lease(self):
        self.q.execute('UPDATE wisp_license_runtime_state SET valid_until=UTC_TIMESTAMP()-INTERVAL 1 SECOND');self.assertEqual(self.reserve(1),-1)
    def test_username_not_bypassed(self):
        self.q.execute("UPDATE wisp_license_info SET status='revoked'")
        for user in ['ping','testbuyer','healthcheck']:self.assertEqual(self.reserve(1,user=user),-1)
    def test_legacy_and_unlimited(self):
        self.q.execute("UPDATE wisp_license_runtime_state SET license_mode='legacy_subscribers',max_active_sessions=NULL")
        self.assertTrue(all(self.reserve(i)==1 for i in range(1,6)))
        self.q.execute("UPDATE wisp_license_runtime_state SET license_mode='active_sessions',max_active_sessions=0")
        self.assertTrue(all(self.reserve(i)==1 for i in range(1,6)))
    def signed_fixture(self, expires_at=None):
        key=Ed25519PrivateKey.generate()
        pem=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        payload={'license_id':'fixture','hardware_id':'ANY','instance_id':'ANY','is_lifetime':True,'limits':{'max_active_sessions':3},'features':{}}
        if expires_at:
            payload.update(is_lifetime=False,expires_at=expires_at)
        raw=json.dumps({'payload':payload,'signature':base64.b64encode(key.sign(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())).decode()})
        self.q.execute('UPDATE wisp_license_info SET raw_package_json=%s,last_heartbeat_at=NOW()', (raw,))
        return pem
    def test_verified_publisher(self):
        pem=self.signed_fixture()
        with patch.object(licensing,'get_master_public_key_pem',return_value=pem):guard.publish_radius_license_state()
        self.assertEqual(self.reserve(1),1)
        self.q.execute("UPDATE wisp_license_info SET raw_package_json='{}'");self.assertEqual(self.reserve(2),-1)
    def test_expired_second_cannot_publish_acceptance(self):
        expires=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(seconds=1)).isoformat()
        pem=self.signed_fixture(expires)
        with patch.object(licensing,'get_master_public_key_pem',return_value=pem):
            guard.publish_radius_license_state()
        self.assertEqual(self.reserve(1),-1)
    def test_missing_heartbeat_cannot_bypass_grace(self):
        pem=self.signed_fixture()
        self.q.execute('UPDATE wisp_license_info SET last_heartbeat_at=NULL,last_verified_at=NOW()-INTERVAL 2 DAY')
        with patch.object(licensing,'get_master_public_key_pem',return_value=pem):
            self.assertFalse(guard.get_active_license_status(force_refresh=True)['valid'])
            guard.publish_radius_license_state()
        self.assertEqual(self.reserve(1),-1)
    def test_capacity_noop_preserves_bounded_verification_lease(self):
        from database.license_capacity import install_capacity_schema
        self.assertEqual(self.reserve(1),1)
        self.q.execute('SELECT is_valid,valid_until,package_hash FROM wisp_license_runtime_state WHERE id=1')
        before=self.q.fetchone()
        install_capacity_schema(self.c)
        self.q.execute('SELECT is_valid,valid_until,package_hash FROM wisp_license_runtime_state WHERE id=1')
        self.assertEqual(before,self.q.fetchone())
        self.assertEqual(self.reserve(2),1)
    def test_capacity_migration_invalidates_old_verification_lease(self):
        from database.license_capacity import install_capacity_schema
        self.assertEqual(self.reserve(1),1)
        self.q.execute('DROP FUNCTION fn_check_license_auth')
        install_capacity_schema(self.c)
        self.assertEqual(self.reserve(2),-1)
    def test_ota_update_reaches_verified_installer(self):
        from unittest.mock import MagicMock
        pem=self.signed_fixture()
        self.q.execute('SELECT raw_package_json FROM wisp_license_info');raw=self.q.fetchone()['raw_package_json']
        response={'valid':True,'status':'active','remote_payload':raw,'remote_action':'sync_license'}
        with patch.object(guard,'authenticated_license_exchange',return_value=response),patch.object(licensing,'get_master_public_key_pem',return_value=pem),patch.object(guard,'_apply_verified_package_to_db') as apply:
            self.assertTrue(guard.sync_license_heartbeat_with_server(max_retries=1)[0]);apply.assert_called_once()
    def test_count_failure_keeps_ui_open(self):
        pem=self.signed_fixture()
        with patch.object(licensing,'get_master_public_key_pem',return_value=pem), patch.object(guard,'get_current_system_counts',return_value=(0,0,-1)), patch.object(guard,'get_pending_reservations_count',return_value=-1):
            result=guard.get_active_license_status(force_refresh=True)
            self.assertTrue(result['valid']);self.assertTrue(result['capacity_read_error']);self.assertIsNone(result['available_capacity']);self.assertIsNone(result['current_active_sessions']);self.assertIsNone(result['pending_reservations'])
            self.assertFalse(guard.check_active_session_quota()[0])
    def test_live_same_account_separate_requests(self):
        first=packet(1,'capacity-a',attrs=[(31,b'AA')]);self.assertEqual(first[0],2)
        self.assertEqual(packet(1,'capacity-a',attrs=[(31,b'AA')])[0],2)
        self.assertEqual(packet(1,'capacity-a',attrs=[(31,b'AA')])[0],2)
        self.assertEqual(packet(1,'capacity-a',attrs=[(31,b'AA')])[0],3)
    def test_live_reject_does_not_reserve(self):
        self.assertEqual(packet(1,'capacity-a',password='wrong')[0],3)
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations');self.assertEqual(self.q.fetchone()['n'],0)
    def test_live_radius_accept_reject_accounting(self):
        auth=secrets.token_bytes(16)
        a=packet(1,'capacity-a',auth,[(31,b'AA')]);self.assertEqual(a[0],2,a)
        self.assertEqual(packet(1,'capacity-a',auth,[(31,b'AA')])[0],2)
        self.assertEqual(packet(1,'capacity-b',attrs=[(31,b'BB')])[0],2)
        self.assertEqual(packet(1,'capacity-c',attrs=[(31,b'CC')])[0],2)
        self.assertEqual(packet(1,'capacity-d',attrs=[(31,b'DD')])[0],3)
        i=20;class_attr=None
        while i<len(a):
            t,n=a[i:i+2]
            if t==25:class_attr=a[i+2:i+n]
            i+=n
        self.assertIsNotNone(class_attr)
        common=[(31,b'AA'),(44,b'live-session'),(25,class_attr)]
        self.assertEqual(packet(4,'capacity-a',attrs=common+[(40,struct.pack('!I',1))])[0],5)
        self.assertEqual(packet(4,'capacity-a',attrs=common+[(40,struct.pack('!I',3)),(42,struct.pack('!I',1000)),(43,struct.pack('!I',2000))])[0],5)
        self.assertEqual(packet(4,'capacity-a',attrs=common+[(40,struct.pack('!I',2)),(42,struct.pack('!I',1500)),(43,struct.pack('!I',2500))])[0],5)
        self.q.execute("SELECT acctinputoctets,acctoutputoctets,acctstoptime FROM radacct WHERE acctsessionid='live-session'");row=self.q.fetchone();self.assertEqual((row['acctinputoctets'],row['acctoutputoctets']),(1500,2500));self.assertIsNotNone(row['acctstoptime'])
        self.assertEqual(packet(1,'capacity-d',attrs=[(31,b'DD')])[0],2)

    def test_expired_pending_reservations_release_capacity(self):
        for i in [1,2,3]:self.assertEqual(self.reserve(i),1)
        self.q.execute("UPDATE wisp_session_reservations SET expires_at=NOW()-INTERVAL 1 SECOND")
        self.assertEqual(guard.get_pending_reservations_count(),0)
        self.assertEqual(guard.get_consumed_license_seats(),0)
        self.assertEqual(self.reserve(4),1)
        self.assertEqual(self.reserve(1),1)
        self.assertEqual(self.reserve(5),1)
        self.assertEqual(self.reserve(6),0)

    def test_stale_session_releases_only_its_seat_and_late_update_restores_count(self):
        for i in [1,2,3]:self.assertEqual(self.reserve(i),1)
        for i in [1,2]:
            self.q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,callingstationid,acctstarttime,acctupdatetime,class,acctinputoctets) VALUES(%s,%s,'capacity-a','10.1.0.1','AA',NOW(),NOW(),%s,1234)",(str(i),str(i),'maxcap:'+f'{i:032x}'))
        self.assertEqual(guard.get_consumed_license_seats(),3)
        self.q.execute("UPDATE radacct SET acctupdatetime=NOW()-INTERVAL 301 SECOND WHERE acctuniqueid='1'")
        self.assertEqual(guard.get_consumed_license_seats(),2)
        self.assertEqual(self.reserve(4),1)
        self.assertEqual(self.reserve(5),0)
        self.q.execute("SELECT acctstoptime,acctinputoctets FROM radacct WHERE acctuniqueid='1'")
        r=self.q.fetchone();self.assertIsNone(r['acctstoptime']);self.assertEqual(r['acctinputoctets'],1234)
        self.q.execute("UPDATE radacct SET acctupdatetime=NOW(),acctinputoctets=5678 WHERE acctuniqueid='1'")
        self.assertEqual(guard.get_consumed_license_seats(),4)
        self.assertEqual(self.reserve(5),0)
        self.q.execute("UPDATE radacct SET acctstoptime=NOW(),acctterminatecause='User-Request' WHERE acctuniqueid='1'")
        self.assertEqual(guard.get_consumed_license_seats(),3)
        self.assertEqual(self.reserve(5),0)
        self.q.execute("SELECT acctstoptime FROM radacct WHERE acctuniqueid='2'");self.assertIsNone(self.q.fetchone()['acctstoptime'])

    def test_start_time_fallback_and_stale_cleanup_never_retain_capacity(self):
        self.assertEqual(self.reserve(1),1)
        self.q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctupdatetime,class) VALUES('fallback','fallback','capacity-a','10.1.0.1',NOW(),NULL,%s)",('maxcap:'+f'{1:032x}',))
        self.assertEqual(guard.get_consumed_license_seats(),1)
        self.q.execute("UPDATE radacct SET acctstarttime=NOW()-INTERVAL 301 SECOND WHERE acctuniqueid='fallback'")
        self.assertEqual(guard.get_consumed_license_seats(),0)
        self.q.execute("UPDATE radacct SET acctstoptime=NOW(),acctterminatecause='Stale-Session-Timeout' WHERE acctuniqueid='fallback'")
        self.assertEqual(guard.get_consumed_license_seats(),0)

    def test_stop_before_start_releases_only_its_pending_seat(self):
        for i in [1,2,3]:self.assertEqual(self.reserve(i),1)
        self.q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,callingstationid,acctstarttime,acctstoptime,acctterminatecause,class) VALUES('stop-first','stop-first','capacity-a','10.1.0.1','AA',NOW(),NOW(),'User-Request',%s)",('maxcap:'+f'{1:032x}',))
        self.q.execute('SELECT COUNT(*) AS n FROM wisp_session_reservations');self.assertEqual(self.q.fetchone()['n'],2)
        self.assertEqual(self.reserve(4),1)

    def test_upgrade_counts_fresh_existing_session_without_retaining_closed_seat(self):
        from database.license_capacity import install_capacity_schema
        self.assertEqual(self.reserve(1),1);self.assertEqual(self.reserve(2),1)
        self.q.execute("INSERT INTO radacct(acctsessionid,acctuniqueid,username,nasipaddress,acctstarttime,acctupdatetime) VALUES('before-upgrade','before-upgrade','existing-before-upgrade','10.1.0.1',NOW(),NOW())")
        install_capacity_schema(self.c)
        self.q.execute("UPDATE wisp_license_runtime_state SET is_valid=1,valid_until=UTC_TIMESTAMP()+INTERVAL 10 MINUTE")
        self.assertEqual(self.reserve(4),0)
        self.q.execute("UPDATE radacct SET acctstoptime=NOW(),acctterminatecause='Stale-Session-Timeout' WHERE acctuniqueid='before-upgrade'")
        self.assertEqual(self.reserve(4),1)

    def test_atomic_resource_limits(self):
        self.q.execute("INSERT IGNORE INTO wisp_roles(id,code,name) VALUES(1,'superadmin','fixture')")
        for table,field in [('wisp_nas_devices','max_nas'),('wisp_managers','max_managers')]:
            self.q.execute('SELECT COUNT(*) AS n FROM '+table);base=self.q.fetchone()['n']
            raw=json.dumps({'payload':{'limits':{field:base+2}}})
            self.q.execute('UPDATE wisp_license_info SET raw_package_json=%s',(raw,))
            self.q.execute('UPDATE wisp_license_runtime_state SET package_hash=SHA2(%s,256),is_valid=1,valid_until=UTC_TIMESTAMP()+INTERVAL 10 MINUTE',(raw,))
            def insert(i):
                c=connect()
                try:
                    with c.cursor() as q:
                        if table=='wisp_nas_devices':q.execute("INSERT INTO wisp_nas_devices(name,ip_address,secret) VALUES(%s,%s,'fixture')",('hardening'+str(i),'192.0.2.'+str(i+1)))
                        else:q.execute("INSERT INTO wisp_managers(username,password_hash,full_name,role_id) VALUES(%s,'fixture','fixture',1)",('hardening'+str(i),))
                    return True
                except pymysql.err.OperationalError as exc:
                    if exc.args[0]!=1644:raise
                    return False
                finally:c.close()
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(insert,range(12)))
            self.assertEqual(results.count(True),2,results)
            self.q.execute("DELETE FROM "+table+" WHERE "+('name' if table=='wisp_nas_devices' else 'username')+" LIKE 'hardening%'")

class GuardTests(unittest.TestCase):
    def test_offline_reactivation_cannot_reset_grace(self):
        package={'payload':{'license_id':'fixture','client_name':'fixture','hardware_id':'ANY','limits':{'max_active_sessions':3}},'signature':'fixture'}
        with patch.object(guard,'ensure_license_tables'),patch.object(guard,'clear_license_cache'),patch.object(guard,'decode_license_string',return_value=(package,None)),patch.object(guard,'get_current_system_counts',return_value=(0,0,0)),patch.object(guard,'verify_license_package',return_value=(True,'valid',{})),patch.object(guard,'get_machine_id',return_value='fixture'),patch.object(guard,'get_instance_uuid',return_value='fixture'),patch.object(guard,'authenticated_license_exchange',side_effect=RuntimeError('offline')),patch.object(guard,'execute_write') as write:
            self.assertFalse(guard.install_and_activate_license('fixture')[0]);write.assert_not_called()
    def test_manager_count_failure_is_closed(self):
        with patch.object(guard,'get_active_license_status',return_value={'valid':True,'max_managers':5}),patch.object(guard,'query_one',side_effect=RuntimeError('offline')):
            self.assertFalse(guard.check_manager_quota()[0])
        with patch.object(guard,'get_active_license_status',return_value={'valid':True,'max_managers':5}),patch.object(guard,'query_one',return_value=None):
            self.assertFalse(guard.check_manager_quota()[0])
    def test_pending_failure(self):
        with patch.object(guard,'get_connection',side_effect=RuntimeError('offline')):
            self.assertEqual(guard.get_pending_reservations_count(),-1)
    def test_pending_in_python_quota(self):
        status={'valid':True,'license_mode':'active_sessions','max_active_sessions':3,'current_active_sessions':1,'total_consumed_capacity':3}
        with patch.object(guard,'get_active_license_status',return_value=status):self.assertFalse(guard.check_active_session_quota()[0])
    def test_unknown_negative_tampered(self):
        key=Ed25519PrivateKey.generate();pem=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        def package(limits):
            p={'hardware_id':'ANY','instance_id':'ANY','is_lifetime':True,'limits':limits}
            data=json.dumps(p,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
            return {'payload':p,'signature':base64.b64encode(key.sign(data)).decode()}
        with patch.object(licensing,'get_master_public_key_pem',return_value=pem):
            self.assertFalse(licensing.verify_license_package(package({}))[0])
            self.assertFalse(licensing.verify_license_package(package({'max_active_sessions':-1}))[0])
            self.assertTrue(licensing.verify_license_package(package({'max_active_sessions':0}))[0])
            self.assertTrue(licensing.verify_license_package(package({'max_subscribers':100}))[0])
            p=package({'max_active_sessions':100});p['payload']['limits']['max_active_sessions']=999
            self.assertFalse(licensing.verify_license_package(p)[0])

if __name__=='__main__':unittest.main(verbosity=2)
