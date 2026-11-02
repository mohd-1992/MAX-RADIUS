"""Host-only shared-component socket. Never mount this socket into tenant Web."""
import importlib.util
from pathlib import Path
import sys

from services.system_component_control import ComponentController


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: component_controller.py PRIVATE_ROOT UNIX_SOCKET')
    # Source checkout and packaged protected image both use the same server.
    try:
        from services.system_update_host import serve
    except ModuleNotFoundError:
        spec=importlib.util.spec_from_file_location('component_socket_server',Path(__file__).with_name('update_controller.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);serve=module.serve
    controller=ComponentController(sys.argv[1]);controller.config()
    serve(controller,sys.argv[2])


if __name__=='__main__':main()
