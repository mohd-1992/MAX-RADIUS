"""Native application startup, with the tested metadata session timeout."""
import sys,subprocess,os
sys.path.insert(0,'/app')
for p in ['/app/storage/backups','/app/storage/uploads','/app/storage/logs']:os.makedirs(p,exist_ok=True)
subprocess.run(['ip','route','replace','192.168.44.0/24','via','172.18.0.10'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
subprocess.run(['python','/opt/max-protection/verify.py'],check=True)
import pymysql
connect=pymysql.connect
def bounded(*a,**kw):
    connection=connect(*a,**kw)
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET SESSION lock_wait_timeout=3')
    except BaseException:
        connection.close()
        raise
    return connection
pymysql.connect=bounded
from run import main
main()
