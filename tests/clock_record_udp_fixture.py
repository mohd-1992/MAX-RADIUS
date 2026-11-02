"""Owned internal-network FreeRADIUS fixture; no NAS or customer connection."""
import hashlib
import hmac
import json
import os
import queue
import secrets
import socket
import struct
import subprocess
import threading
import time
import uuid


class RadiusFixture:
    def __init__(self,h):
        self.h=h;self.container=None;self.network=None;self.client=None;self.relay=None
        self.secret=secrets.token_hex(24).encode();self.metrics=[]
        self.phase='baseline'
        self.replies=queue.Queue();self.relay_lock=threading.Lock()

    def command(self,args):
        p=subprocess.run(args,capture_output=True,timeout=30)
        with (self.h.REPORT/'udp-setup.private.log').open('ab') as f:f.write(p.stdout+p.stderr)
        if p.returncode:raise RuntimeError('Isolated UDP setup failed; see private log')
        return p.stdout.decode().strip()

    def start(self):
        h=self.h;folder=h.REPORT/'radius-config';folder.mkdir()
        # Pin the actual local binary identity; do not assume tag equality with the customer.
        self.image=self.command(['docker','image','inspect','--format','{{.Id}}','max-radius-core:first-use-local-20261013'])
        sql=(h.ROOT/'docker/freeradius/mods-available/sql').read_text()
        sql=sql.replace('server = "mariadb"','server = "fixture-db"').replace('login = "radius"','login = "root"')
        sql=sql.replace('password = "radpass"','password = "'+h.password+'"').replace('radius_db = "radius_wisp"','radius_db = "'+h.schema+'"')
        sql=sql.replace('read_clients = yes','read_clients = no').replace('start = 5','start = 2').replace('min = 4','min = 2')
        if getattr(h,'CASE_MODULE','')=='clock_boot_resilience_cases':
            sql=sql.replace('retry_delay = 30','retry_delay = 1')
        sql=sql.replace('dialect = "mysql"','dialect = "mysql"\n    group_attribute = "SQL-Group"\n    sql_user_name = "%{User-Name}"')
        sql=sql.replace('$INCLUDE ${modconfdir}/${.:name}/main/mysql/queries.conf','$INCLUDE /fixture/queries.conf')
        sql+='\n'
        (folder/'sql').write_text(sql,encoding='utf-8')
        (folder/'queries.conf').write_text(h.raw['private_query_source'],encoding='utf-8')
        (folder/'postauth.conf').write_text((h.ROOT/'docker/freeradius/license-capacity-postauth.conf').read_text(),encoding='utf-8')
        (folder/'clients.conf').write_text('client isolated {\n ipaddr = 0.0.0.0/0\n secret = '+self.secret.decode()+'\n require_message_authenticator = yes\n}\n',encoding='utf-8')
        site='''server default {
 listen { type = auth
 ipaddr = *
 port = 1812
 }
 listen { type = acct
 ipaddr = *
 port = 1813
 }
 authorize { sql
 pap
 }
 authenticate {
 Auth-Type PAP {
 pap
 }
 }
 preacct {
 acct_unique
 }
 accounting {
 sql
 }
 post-auth {
 $INCLUDE /fixture/postauth.conf
 sql
 update reply {
 Message-Authenticator := 0x00000000000000000000000000000000
 }
 Post-Auth-Type REJECT {
 sql
 update reply {
 Message-Authenticator := 0x00000000000000000000000000000000
 }
 }
 }
}
'''
        (folder/'default').write_text(site,encoding='utf-8')
        self.network='max-clock-radius-'+uuid.uuid4().hex[:10]
        self.command(['docker','network','create','--internal','--label','max-owned-task=clock-record-priority-lab',self.network])
        self.command(['docker','network','connect','--alias','fixture-db',self.network,h.container])
        name='max-clock-radius-'+uuid.uuid4().hex[:10]
        self.container=self.command(['docker','create','--name',name,'--label','max-owned-task=clock-record-priority-lab',
            '--network',self.network,'--network-alias','fixture-radius','--restart=no',
            '--entrypoint','sh',self.image,'-c',
            'chmod -R go-w /fixture; chown -R freerad:freerad /fixture; '
            'chmod 640 /etc/freeradius/3.0/mods-available/sql /etc/freeradius/3.0/sites-available/default /etc/freeradius/3.0/clients.conf; '
            'chown freerad:freerad /etc/freeradius/3.0/mods-available/sql /etc/freeradius/3.0/sites-available/default /etc/freeradius/3.0/clients.conf; '
            'ln -sf ../mods-available/sql /etc/freeradius/3.0/mods-enabled/sql; exec freeradius -f -l stdout'])
        self.command(['docker','cp',str(folder)+ '/.',self.container+':/fixture'])
        for src,dest in [('sql','mods-available/sql'),('default','sites-available/default'),('clients.conf','clients.conf')]:
            self.command(['docker','cp',str(folder/src),self.container+':/etc/freeradius/3.0/'+dest])
        self.command(['docker','start',self.container])
        for _ in range(40):
            logs=self.command(['docker','logs',self.container])
            if 'Ready to process requests' in logs or 'Ready to process requests' in (h.REPORT/'udp-setup.private.log').read_text(errors='replace'):break
            state=self.command(['docker','inspect','--format','{{.State.Running}}',self.container])
            if state!='true':raise RuntimeError('Local RADIUS did not start; see private logs')
            time.sleep(.2)
        # No host UDP publication. One persistent Python relay runs on the same internal network.
        self.client=self.command(['docker','run','-d','--name','max-clock-udp-client-'+uuid.uuid4().hex[:10],
            '--label','max-owned-task=clock-record-priority-lab','--network',self.network,'--restart=no',
            '--entrypoint','sleep','max-radius-clock-guard-lab:20261017','infinity'])
        relay_code='''import json,socket,sys
for line in sys.stdin:
 try:
  item=json.loads(line)
  with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
   sock.settimeout(2)
   sock.sendto(bytes.fromhex(item['packet']),('fixture-radius',1813 if item['code']==4 else 1812))
   data=sock.recv(65535)
  print(json.dumps({'data':data.hex()}),flush=True)
 except Exception as ex:print(json.dumps({'error':type(ex).__name__}),flush=True)
'''
        self.relay_error=(h.REPORT/'udp-relay.stderr.private').open('w',encoding='utf-8')
        self.relay=subprocess.Popen(['docker','exec','-i',self.client,'python3','-u','-c',relay_code],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.relay_error,text=True,encoding='utf-8')
        def reader():
            for line in self.relay.stdout:self.replies.put(line)
            self.replies.put(None)
        threading.Thread(target=reader,daemon=True).start()
        # Non-debug FreeRADIUS does not always log Ready; a live listener is tested by the first packet.

    @staticmethod
    def attr(kind,value):
        if isinstance(value,str):value=value.encode()
        return bytes([kind,len(value)+2])+value

    def request(self,user, *, password='fixture-pass', status=None, session=None, seconds=0,up=0,down=0,slot=None):
        started=time.monotonic();code=4 if status else 1;ident=os.urandom(1)[0]
        auth=b'\0'*16 if status else os.urandom(16)
        a=self.attr(1,user)+self.attr(4,socket.inet_aton('192.0.2.99'))+self.attr(5,struct.pack('!I',1))+self.attr(31,'02:00:00:00:99:99')
        if status:
            a+=self.attr(40,struct.pack('!I',status))+self.attr(44,session)+self.attr(46,struct.pack('!I',seconds))
            a+=self.attr(42,struct.pack('!I',up))+self.attr(43,struct.pack('!I',down))+self.attr(8,socket.inet_aton('198.51.100.99'))
            if slot:a+=self.attr(25,slot)
            if status==2:a+=self.attr(49,struct.pack('!I',1))
        else:
            clear=password.encode();clear+=b'\0'*((16-len(clear)%16)%16);enc=b'';prev=auth
            for pos in range(0,len(clear),16):
                mask=hashlib.md5(self.secret+prev).digest();prev=bytes(x^y for x,y in zip(clear[pos:pos+16],mask));enc+=prev
            a+=self.attr(2,enc)+self.attr(80,b'\0'*16)
        header=struct.pack('!BBH',code,ident,20+len(a))
        if code==1:
            ma=hmac.new(self.secret,header+auth+a,hashlib.md5).digest();a=a[:-16]+ma
        else:auth=hashlib.md5(header+auth+a+self.secret).digest()
        packet=header+auth+a
        with self.relay_lock:
            self.relay.stdin.write(json.dumps({'packet':packet.hex(),'code':code})+'\n');self.relay.stdin.flush()
            response=self.replies.get(timeout=3)
            if response is None:raise RuntimeError('Isolated UDP relay ended')
            reply=json.loads(response)
            if 'error' in reply:raise RuntimeError('Isolated UDP request failed: '+reply['error'])
            data=bytes.fromhex(reply['data'])
        assert data[1]==ident and len(data)==struct.unpack('!H',data[2:4])[0]
        assert hmac.compare_digest(data[4:20],hashlib.md5(data[:4]+auth+data[20:]+self.secret).digest())
        out={};pos=20
        while pos<len(data):
            kind,n=data[pos:pos+2];assert n>=2;out[kind]=data[pos+2:pos+n];pos+=n
        self.metrics.append({'request_code':code,'response_code':data[0],'phase':self.phase,'ms':round((time.monotonic()-started)*1000,3)})
        return data[0],out

    def stop(self):
        if self.relay:
            self.relay.stdin.close()
            try:self.relay.wait(timeout=5)
            except subprocess.TimeoutExpired:self.relay.terminate()
            self.relay_error.close()
        if self.client:self.command(['docker','stop',self.client])
        if self.container:
            p=subprocess.run(['docker','logs',self.container],capture_output=True,timeout=20)
            (self.h.REPORT/'radius.stdout.private').write_bytes(p.stdout)
            (self.h.REPORT/'radius.stderr.private').write_bytes(p.stderr)
            self.command(['docker','stop',self.container])
        (self.h.REPORT/'radius-fixture.private.json').write_text(json.dumps({'container':self.container,'client':self.client,'network':self.network,'image':getattr(self,'image',None),'host_udp_ports_published':False,'packets':self.metrics},indent=2))
