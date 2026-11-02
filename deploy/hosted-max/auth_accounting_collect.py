"""One bounded read-only observation. Input/output JSON stays in private storage.

Execute on the hosting VPS via a pinned SSH transport. Never imports MAX, starts
workers, sends RADIUS/CoA, modifies databases, installs services, or schedules itself.
"""
import hashlib
import ipaddress
import json
import re
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

TARGETS = frozenset(('max1', 'm'))
ROW_LIMIT = 2000
TABLES = frozenset(('radpostauth','radacct','user_audit_logs','wisp_audit_logs',
                    'wisp_invoices','wisp_voucher_sales'))
DB_COMMAND = ('export MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}"; '
    'exec mariadb --default-character-set=utf8mb4 -uroot -N -B --raw "${MYSQL_DATABASE:-$MARIADB_DATABASE}" -e "$1"')


def literal(value):
    data = str(value).encode('utf-8').hex()
    return "CONVERT(0x"+data+" USING utf8mb4) COLLATE utf8mb4_unicode_ci" if data else "''"


def ids(values):
    values = list(values)
    if len(values) > ROW_LIMIT or any(type(v) is not int or v <= 0 for v in values):
        raise ValueError('Invalid or excessive session identifiers')
    return ','.join(map(str, values)) or '0'


class ReadOnlyDB:
    def __init__(self, tenant):
        if tenant not in TARGETS:
            raise ValueError('Target is not allowlisted')
        self.tenant = tenant
        self.errors=[]

    def query(self, sql):
        # No user SQL, stored routines, OUTFILE, stacked statements or locking reads.
        if (not sql.startswith('SELECT ') or ';' in sql or
                re.search(r'\b(INTO|OUTFILE|DUMPFILE|FOR\s+UPDATE|SLEEP|GET_LOCK|BENCHMARK)\b',sql,re.I)):
            raise ValueError('Not a permitted observation query')
        statement = ("SET SESSION max_statement_time=3;SET SESSION lock_wait_timeout=1;"
            "SET SESSION innodb_lock_wait_timeout=1;SET time_zone='+03:00';"
            'START TRANSACTION READ ONLY;'+sql+';ROLLBACK;')
        p = subprocess.run(['docker','exec','max_radius_'+self.tenant+'_db','sh','-c',
                            DB_COMMAND,'sh',statement],capture_output=True,text=True,timeout=12)
        if p.returncode or len(p.stdout.encode()) > 12_000_000:
            self.errors.append({'query_sha256':hashlib.sha256(sql.encode()).hexdigest(),
                                'diagnostic':p.stderr[-1200:]})
            raise RuntimeError('Read-only observation failed or exceeded its budget')
        return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]

    def tail(self, table, fields, cursor):
        if table not in TABLES:
            raise ValueError('Table is not allowlisted')
        cursor = int(cursor or 0)
        if cursor < 0:
            raise ValueError('Invalid cursor')
        # A bounded overlap catches ordinary delayed commits; saturation is explicit.
        if cursor:
            cutoff = max(0, cursor-500)
        else:
            cutoff = self.query('SELECT JSON_OBJECT(\'n\',GREATEST(COALESCE(MAX(id),0)-1500,0)) FROM '+table)[0]['n'] if table != 'radacct' else 0
        pk = 'radacctid' if table == 'radacct' else 'id'
        if table == 'radacct' and not cursor:
            cutoff=self.query("SELECT JSON_OBJECT('n',GREATEST(COALESCE(MAX(radacctid),0)-1500,0)) FROM radacct")[0]['n']
        rows=self.query('SELECT JSON_OBJECT('+fields+') FROM '+table+' WHERE '+pk+'>'+str(cutoff)+' ORDER BY '+pk+' LIMIT '+str(ROW_LIMIT+1))
        return rows[:ROW_LIMIT],len(rows)<=ROW_LIMIT


class RouterReadOnly:
    """Bounded RouterOS binary API with a fixed list of read commands."""
    COMMANDS = {
        '/ip/hotspot/active/print': 'user,address,mac-address,session-id,uptime,radius,.flags,login-by,bytes-in,bytes-out',
        '/ppp/active/print': 'name,address,caller-id,session-id,uptime,radius,service',
        '/ppp/aaa/print': 'interim-update',
        '/ip/hotspot/profile/print': 'name,use-radius,radius-interim-update',
    }

    def __init__(self, host, port, username, password):
        ipaddress.IPv4Address(host)
        if not 1 <= int(port) <= 65535:
            raise ValueError('Invalid API port')
        self.host,self.port,self.username,self.password=host,int(port),username,password
        self.sock=None;self.deadline=0

    def exact(self,n):
        if n>1_000_000:raise ValueError('Router reply word too large')
        data=bytearray()
        while len(data)<n:
            remaining=self.deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('NAS observation budget exceeded')
            self.sock.settimeout(min(3,remaining))
            piece=self.sock.recv(n-len(data))
            if not piece:raise ConnectionError('Truncated router response')
            data.extend(piece)
        return bytes(data)

    def read_length(self):
        b=self.exact(1)[0]
        if b<0x80:return b
        if b<0xc0:return ((b&0x3f)<<8)|self.exact(1)[0]
        if b<0xe0:return ((b&0x1f)<<16)|int.from_bytes(self.exact(2),'big')
        if b<0xf0:return ((b&0x0f)<<24)|int.from_bytes(self.exact(3),'big')
        if b==0xf0:return int.from_bytes(self.exact(4),'big')
        raise ValueError('Invalid router word length')

    @staticmethod
    def encode(word):
        b=word.encode();n=len(b)
        if n<0x80:prefix=bytes([n])
        elif n<0x4000:prefix=struct.pack('!H',n|0x8000)
        elif n<0x200000:prefix=struct.pack('!I',n|0xc00000)[1:]
        elif n<0x10000000:prefix=struct.pack('!I',n|0xe0000000)
        else:raise ValueError('Request too large')
        return prefix+b

    def exchange(self,words):
        self.sock.sendall(b''.join(self.encode(w) for w in words)+b'\0')
        result=[];failed=False;words_seen=0
        while True:
            sentence=[]
            while True:
                n=self.read_length()
                if not n:break
                words_seen+=1
                if words_seen>100_000:raise ValueError('Router reply budget exceeded')
                sentence.append(self.exact(n).decode('utf-8','strict'))
            if not sentence:raise ConnectionError('Missing RouterOS sentence')
            attrs={w[1:].split('=',1)[0]:w[1:].split('=',1)[1] for w in sentence[1:] if w.startswith('=') and '=' in w[1:]}
            if sentence[0] in ('!trap','!fatal'):failed=True
            if sentence[0]=='!fatal':raise ConnectionError('Router API fatal error')
            if sentence[0]=='!re':result.append(attrs)
            if len(result)>ROW_LIMIT:raise ValueError('NAS results truncated by safety limit')
            if sentence[0]=='!done':
                if failed:raise PermissionError('Router API rejected read command')
                return result,attrs

    def __enter__(self):
        self.deadline=time.monotonic()+12
        self.sock=socket.create_connection((self.host,self.port),timeout=3)
        try:self.exchange(['/login','=name='+self.username,'=password='+self.password])
        except BaseException:
            self.sock.close();raise
        return self

    def read(self,command):
        if command not in self.COMMANDS:raise ValueError('Router write/control command forbidden')
        # Some RouterOS builds omit flags/uptime when a proplist is supplied.
        # The active table contains no passwords; only selected fields leave this reader.
        if command=='/ip/hotspot/active/print':return self.exchange([command])[0]
        return self.exchange([command,'=.proplist='+self.COMMANDS[command]])[0]

    def __exit__(self,*unused):
        if self.sock:self.sock.close()


def duration(value):
    s=str(value or '')
    if s=='0':return 0
    if re.fullmatch(r'(?:(\d+)d)?\d+:\d{2}:\d{2}',s):
        d,rest=(s.split('d',1) if 'd' in s else ('0',s));parts=list(map(int,rest.split(':')))
        return int(d)*86400+parts[0]*3600+parts[1]*60+parts[2]
    pieces=re.findall(r'(\d+)([wdhms])',s)
    if not pieces or ''.join(n+u for n,u in pieces)!=s:return None
    return sum(int(n)*{'w':604800,'d':86400,'h':3600,'m':60,'s':1}[u] for n,u in pieces)


def nas_observation(config, credentials):
    """Called inside the tenant Web network, never using MAX imports."""
    result=[];intervals=[];errors=[];metadata=[]
    allowed=config['nas']
    for n in credentials:
        ip=n['ip']
        if ip not in allowed or int(n['port'])!=int(allowed[ip]):
            errors.append({'nas':ip,'reason':'unapproved_nas_identity'});continue
        try:
            with RouterReadOnly(ip,n['port'],n['username'],n['password']) as api:
                for command,kind in (('/ip/hotspot/active/print','hotspot'),('/ppp/active/print','pppoe')):
                    for live in api.read(command):
                        if not any(m['kind']==kind and m['nas']==ip for m in metadata):
                            metadata.append({'nas':ip,'kind':kind,'properties':sorted(live),'flags':live.get('.flags'),'login_by':live.get('login-by')})
                        radius=live.get('radius')
                        if radius not in ('true','false','yes','no'):
                            errors.append({'nas':ip,'reason':'radius_origin_unavailable'})
                        result.append({'nas':ip,'kind':kind,'user':live.get('user',live.get('name')),
                            'sid':live.get('session-id'),'ip':live.get('address'),
                            'mac':live.get('mac-address',live.get('caller-id')),
                            'radius':True if radius in ('true','yes') else False if radius in ('false','no') else None,
                            'uptime':duration(live.get('uptime')),
                            'input':int(live['bytes-in']) if 'bytes-in' in live else None,
                            'output':int(live['bytes-out']) if 'bytes-out' in live else None})
                for command in ('/ppp/aaa/print','/ip/hotspot/profile/print'):
                    for item in api.read(command):
                        val=duration(item.get('interim-update',item.get('radius-interim-update')))
                        if val and val>0:intervals.append(val)
        except Exception as exc:
            errors.append({'nas':ip,'reason':type(exc).__name__})
    if set(allowed)!={n['ip'] for n in credentials}:
        errors.append({'reason':'configured_nas_set_changed'})
    return {'sessions':result,'interval_seconds':max(intervals) if intervals else None,'errors':errors,'metadata':metadata}


def binding(config):
    tenant=config['tenant']
    if tenant not in TARGETS:raise ValueError('Tenant not allowlisted')
    storage=Path('/opt/clients')/tenant/'storage'
    if (storage/'instance_id').read_text().strip()!=config['instance']:
        raise ValueError('Installation identity changed')
    p=subprocess.run(['docker','inspect','max_radius_'+tenant+'_web','max_radius_'+tenant+'_db'],capture_output=True,text=True,timeout=10,check=True)
    for item in json.loads(p.stdout):
        if item['Config'].get('Labels',{}).get('com.docker.compose.project')!=config['project']:
            raise ValueError('Container project mismatch')
        if item['Name'].endswith('_web') and not any(m.get('Source')==str(storage) and m.get('Destination')=='/app/storage' for m in item['Mounts']):
            raise ValueError('Tenant storage binding changed')


def collect(config, hints=None, deep=False):
    binding(config);hints=hints or {};db=ReadOnlyDB(config['tenant'])
    snap={'schema':1,'tenant':config['tenant'],'instance':config['instance'],'coverage':{},
          'auth':[],'sessions':[],'accounts':[],'operations':[],'nas_sessions':[],
          'interim_seconds':config.get('interim_seconds',900),'cursors':dict(hints.get('cursors',{}))}
    snap['epoch']=db.query("SELECT JSON_OBJECT('epoch',UNIX_TIMESTAMP())")[0]['epoch']
    def cover(area,ok,reason='complete'):
        snap['coverage'][area]={'complete':bool(ok),'reason':reason if not ok else 'complete'}
    cursors=hints.get('cursors',{})
    def tail(table,fields):
        r,ok=db.tail(table,fields,cursors.get(table))
        snap['cursors'][table]=max([x['id'] for x in r]+[cursors.get(table,0)])
        return r,ok
    try:
        r,ok=tail('radpostauth',"'id',id,'user',username,'reply',reply,'at',UNIX_TIMESTAMP(authdate),'nas',nasipaddress,'mac',callingstationid")
        snap['auth']=[x for x in r if (x.get('at') or 0)>=snap['epoch']-3600];cover('auth',ok,'row_limit')
    except Exception as exc:cover('auth',False,type(exc).__name__)
    fields=("'id',a.radacctid,'user',a.username,'nas',a.nasipaddress,'sid',a.acctsessionid,"
        "'uid',a.acctuniqueid,'class',a.class,'ip',a.framedipaddress,'mac',a.callingstationid,"
        "'start',UNIX_TIMESTAMP(a.acctstarttime),'updated',UNIX_TIMESTAMP(a.acctupdatetime),"
        "'stop',UNIX_TIMESTAMP(a.acctstoptime),'input',a.acctinputoctets,'output',a.acctoutputoctets,"
        "'seconds',a.acctsessiontime,'cause',a.acctterminatecause")
    try:
        # Recent IDs, all open rows, and explicitly retained IDs have different evidence roles.
        r,ok=tail('radacct',fields.replace('a.',''))
        opened=db.query('SELECT JSON_OBJECT('+fields+') FROM radacct a WHERE a.acctstoptime IS NULL ORDER BY a.radacctid LIMIT '+str(ROW_LIMIT+1))
        checked=list(dict.fromkeys(int(v) for v in hints.get('session_ids',[])))
        retained=db.query('SELECT JSON_OBJECT('+fields+') FROM radacct a WHERE a.radacctid IN ('+ids(checked)+')') if checked else []
        snap['checked_session_ids']=checked
        recent=[x for x in r if max(x.get('start') or 0,x.get('updated') or 0,x.get('stop') or 0)>=snap['epoch']-3600]
        snap['sessions']=list({x['id']:x for x in recent+opened[:ROW_LIMIT]+retained}.values())
        if len(snap['sessions'])>ROW_LIMIT:
            snap['sessions']=sorted(snap['sessions'],key=lambda x:(x.get('stop') is not None,-x['id']))[:ROW_LIMIT];ok=False
        cover('sessions',ok and len(opened)<=ROW_LIMIT,'row_limit')
    except Exception as exc:cover('sessions',False,type(exc).__name__)
    try:
        operations=[];ok=True
        specs=(('user_audit_logs',"'id',id,'kind','audit','user',username,'action',action,'at',UNIX_TIMESTAMP(created_at)"),
            ('wisp_audit_logs',"'id',id,'kind','admin_audit','action',action,'at',UNIX_TIMESTAMP(created_at)"),
            ('wisp_invoices',"'id',id,'kind','invoice','number',invoice_number,'account_id',subscriber_id,'amount_cents',CAST(amount*100 AS SIGNED),'status',status,'at',UNIX_TIMESTAMP(created_at)"),
            ('wisp_voucher_sales',"'id',id,'kind','sale','user',username,'voucher_id',voucher_id,'first_activation',first_activation_voucher_id,'amount_cents',CAST(price*100 AS SIGNED),'at',UNIX_TIMESTAMP(activated_at)"))
        for table,expr in specs:
            got,full=tail(table,expr);operations.extend(x for x in got if (x.get('at') or 0)>=snap['epoch']-3600);ok=ok and full
        snap['operations']=operations;cover('operations',ok,'row_limit')
    except Exception as exc:cover('operations',False,type(exc).__name__)
    try:
        users={x['user'] for x in snap['sessions']+snap['auth']+snap['operations'] if x.get('user')}
        if len(users)>1000:raise ValueError('Account coverage limit')
        names=','.join(literal(x) for x in sorted(users)) or "''"
        accounts=[]
        for table,kind in (('wisp_subscribers','subscriber'),('wisp_vouchers','voucher')):
            reason="COALESCE(s.pause_reason,'')='تعذر تسوية حدود دورة المحاسبة'"
            if kind=='voucher':reason+=' OR '+"COALESCE(s.expire_reason,'')='تعذر تسوية حدود دورة المحاسبة'"
            loan='s.loan_balance_mb' if kind=='subscriber' else '0'
            maximum='p.simultaneous_sessions' if kind=='subscriber' else 'COALESCE(s.snap_simultaneous_sessions,p.simultaneous_sessions)'
            invoice_ids=ids(list({o['account_id'] for o in snap['operations'] if o.get('kind')=='invoice' and o.get('account_id')})) if kind=='subscriber' else '0'
            sql=("SELECT JSON_OBJECT('kind',"+literal(kind)+",'id',s.id,'user',s.username,'status',s.status,"
                "'package_id',s.package_id,'cycle',UNIX_TIMESTAMP(COALESCE(s.last_renewed_at,s.first_used_at,s.created_at)),"
                "'expires',UNIX_TIMESTAMP(s.expires_at),'quota_mb',COALESCE(s.snap_volume_quota_mb,p.volume_quota_mb),'extra_mb',s.extra_quota_mb,"
                "'loan',"+loan+",'balance_cents',CAST(s.balance*100 AS SIGNED),'simultaneous',"+maximum+","
                "'uncertain',(s.status IN ('suspended','disabled') AND ("+reason+"))) FROM "+table+' s LEFT JOIN wisp_packages p ON p.id=s.package_id'+
                ' WHERE s.username IN ('+names+') OR ('+reason+') OR s.id IN ('+invoice_ids+') LIMIT 1001')
            got=db.query(sql)
            if len(got)>1000:raise ValueError('Account coverage limit')
            accounts.extend(got)
        snap['accounts']=accounts
        if len({a['user'] for a in accounts})!=len(accounts):raise ValueError('Ambiguous account ownership')
        by={a['user']:a for a in accounts}
        for r in snap['sessions']:
            r['cycle']=by.get(r['user'],{}).get('cycle')
        baselines=db.query("SELECT JSON_OBJECT('id',b.radacctid,'renewed',UNIX_TIMESTAMP(b.renewed_at),'input',b.baseline_input_bytes,'output',b.baseline_output_bytes,'seconds',b.baseline_seconds) FROM wisp_session_baselines b WHERE b.radacctid IN ("+ids([r['id'] for r in snap['sessions']])+')') if snap['sessions'] else []
        base={(b['id'],b['renewed']):b for b in baselines}
        for r in snap['sessions']:
            b=base.get((r['id'],r.get('cycle')));r['has_baseline']=b is not None
            if b:r['baseline']={k:b[k] for k in ('input','output','seconds')}
        # An account's whole current cycle, not just the retained session sample.
        for kind,table in (('subscriber','wisp_subscribers'),('voucher','wisp_vouchers')):
            agg=("SELECT JSON_OBJECT('user',s.username,'bytes',SUM(CASE WHEN a.acctstarttime>=COALESCE(s.last_renewed_at,s.first_used_at,s.created_at) THEN COALESCE(a.acctinputoctets,0)+COALESCE(a.acctoutputoctets,0) WHEN b.radacctid IS NOT NULL THEN GREATEST(0,COALESCE(a.acctinputoctets,0)-b.baseline_input_bytes)+GREATEST(0,COALESCE(a.acctoutputoctets,0)-b.baseline_output_bytes) ELSE 0 END),'unknown',SUM(a.acctstarttime<COALESCE(s.last_renewed_at,s.first_used_at,s.created_at) AND (a.acctstoptime IS NULL OR a.acctstoptime>COALESCE(s.last_renewed_at,s.first_used_at,s.created_at)) AND b.radacctid IS NULL)) FROM "+table+" s JOIN radacct a ON a.username=s.username LEFT JOIN wisp_session_baselines b ON b.radacctid=a.radacctid AND b.renewed_at=COALESCE(s.last_renewed_at,s.first_used_at,s.created_at) WHERE s.username IN ("+names+') GROUP BY s.username')
            for a in db.query(agg):
                if a['user'] in by:
                    by[a['user']]['used_bytes']=None if a['unknown'] else a['bytes']
                    by[a['user']]['uncertain']=bool(a['unknown'] or by[a['user']].get('uncertain'))
        invoice_users={a['id']:a['user'] for a in accounts if a['kind']=='subscriber'}
        for op in snap['operations']:
            if op.get('kind')=='invoice':op['user']=invoice_users.get(op.get('account_id'))
        pending=db.query("SELECT JSON_OBJECT('user',username,'age',TIMESTAMPDIFF(SECOND,updated_at,NOW())) FROM wisp_online_policy_outbox WHERE status='pending' AND username IN ("+names+') LIMIT 1001')
        for row in pending:
            if row['user'] in by:by[row['user']]['policy_pending_age']=row['age']
        # A per-user RADIUS override takes precedence over the package default.
        overrides=db.query("SELECT JSON_OBJECT('user',username,'value',value) FROM radcheck WHERE attribute='Simultaneous-Use' AND username IN ("+names+') LIMIT 2001')
        for row in overrides:
            if row['user'] in by and str(row['value']).isdigit():
                by[row['user']]['simultaneous']=int(row['value'])
                by[row['user']]['simultaneous_source']='radcheck'
        cover('accounts',True)
    except Exception as exc:cover('accounts',False,type(exc).__name__)
    if not deep:
        cover('nas',False,'deferred_until_deep_pass')
    else:
        try:
            nas=db.query("SELECT JSON_OBJECT('ip',ip_address,'port',api_port,'username',api_username,'password',api_password) FROM wisp_nas_devices ORDER BY id LIMIT 33")
            if len(nas)>32:raise ValueError('NAS coverage limit')
            # Only the standalone reader is copied into the existing Web network namespace.
            own=Path(__file__).read_text() if '__file__' in globals() and Path(__file__).is_file() else globals().get('_MONITOR_SOURCE')
            if not own:raise RuntimeError('Standalone reader source missing')
            import ast
            tree=ast.parse(own)
            definitions='\n\n'.join(ast.get_source_segment(own,n) for n in tree.body
                if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in ('RouterReadOnly','duration','nas_observation'))
            inner='import socket,struct,time,re,ipaddress,json,sys\nROW_LIMIT=2000\n'+definitions+'\nv=json.load(sys.stdin)\nprint(json.dumps(nas_observation(v["config"],v["credentials"])))\n'
            p=subprocess.run(['docker','exec','-i','-e','PYTHONDONTWRITEBYTECODE=1','max_radius_'+config['tenant']+'_web','python','-B','-c',inner],input=json.dumps({'config':config,'credentials':nas}),capture_output=True,text=True,timeout=45)
            if p.returncode:raise RuntimeError('NAS reader failed')
            observed=json.loads(p.stdout);snap['nas_sessions']=observed['sessions']
            if observed.get('interval_seconds'):snap['interim_seconds']=observed['interval_seconds']
            cover('nas',not observed['errors'],'nas_read_incomplete')
            snap['nas_errors']=observed['errors']
            snap['nas_metadata']=observed['metadata']
        except Exception as exc:cover('nas',False,type(exc).__name__)
    snap['query_diagnostics']=db.errors
    return snap


def main():
    envelope=json.load(sys.stdin)
    value=collect(envelope['config'],envelope.get('hints'),envelope.get('deep') is True)
    print(json.dumps(value,ensure_ascii=True))


if __name__=='__main__':main()
