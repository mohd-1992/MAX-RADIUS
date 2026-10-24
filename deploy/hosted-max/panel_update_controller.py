"""Host controller for pre-control installations. Started only on explicit click."""
import importlib.util
import json
from pathlib import Path
import re
import sys

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE/'update_support'))
from services.system_update_runtime import write_json

spec=importlib.util.spec_from_file_location('panel_update_host',HERE/'update_support'/'update_controller.py')
host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)


class Controller(host.Controller):
    def launch(self, config, old, folder, job):
        from panel_updates import command
        # Fixed program and validated root/name; no image, command or env from HTTP.
        name=config['project']
        command(['systemd-run','--quiet','--collect','--unit=max-panel-update-job-'+name+'-'+job,
                 '--property=Type=exec','--property=UMask=0077','--property=Restart=on-failure',
                 '--property=RestartSec=2s','--property=StartLimitBurst=2',
                 sys.executable,str(HERE/'panel_update_controller.py'),'work',name,job])


def main():
    from panel_updates import context
    if len(sys.argv) not in (3,4) or sys.argv[1] not in ('serve','work'): raise SystemExit(2)
    action,name=sys.argv[1:3];cfg,path,instance=context(name)
    root=Path(cfg['state_root'])/name
    proof=json.loads((root/'panel-controller.private.json').read_text())
    if proof!={'status':'ready','instance':instance}:raise SystemExit('Enrollment requires review')
    controller=Controller(root);controller.config()
    if action=='serve' and len(sys.argv)==3:
        host.serve(controller,Path(cfg['socket_root'])/name/'controller.sock')
    elif action=='work' and len(sys.argv)==4 and re.fullmatch('[0-9a-f]{32}',sys.argv[3]):
        host.worker(root,sys.argv[3])
    else:raise SystemExit(2)


if __name__=='__main__':main()
