"""Install only the scoped socket link in an existing, verified tenant Web."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tenant_docker_proxy import Scope


def activate(config):
    scope = Scope(config)
    binding, services = scope.state()
    path = Path(config['socket'])
    for _ in range(25):
        if path.is_socket():
            break
        time.sleep(0.2)
    if not path.is_socket():
        raise RuntimeError('Scoped control socket did not become available')
    target = '/app/storage/.host-control/docker.sock'
    code = ("import os\np='/var/run/docker.sock'\nt=" + repr(target) +
            "\nif os.path.lexists(p):\n"
            " assert os.path.islink(p) and os.readlink(p)==t, 'Existing Docker socket is not owned by scoped control'\n"
            "else:\n os.symlink(t,p)\n")
    subprocess.run(['docker', 'exec', '--user', '0', services['web']['Id'],
                    'python', '-c', code], check=True, timeout=15,
                   stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    activate(json.loads(Path(sys.argv[1]).read_text()))
