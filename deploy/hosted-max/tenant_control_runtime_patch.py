"""Version-checked, idempotent integration for the max-r host executor source.

Caller owns deployment, backup, queue admission, and executor reload. Importing
this module does not touch a host, database, license, or runtime file.
"""
import ast


def patch_runtime(source):
    marker = "MAX_CONTROL_TENANT_ID=row['id']"
    if marker in source:
        if 'from tenant_control_lifecycle import ensure' not in source or 'from tenant_control_lifecycle import remove' not in source:
            raise ValueError('Partial control integration requires review')
        return source
    anchors = ('        return env\n', '    def create_container(self,row,service,image=None):\n',
               "        import shutil\n        source=self.path(row['slug']).resolve();destination=")
    if any(source.count(s) != 1 for s in anchors):
        raise ValueError('Executor version differs; no changes applied')
    source = source.replace(anchors[0],
        "        if service == 'web' and self.config.get('tenant_control'):\n"
        "            env.update(MAX_CONTROL_MODE='hosted', MAX_CONTROL_SOCKET='/app/storage/.host-control/docker.sock', MAX_CONTROL_TENANT_ID=row['id'])\n"
        + anchors[0], 1)
    source = source.replace(anchors[1], anchors[1] +
        "        if service == 'web' and self.config.get('tenant_control'):\n"
        "            from tenant_control_lifecycle import ensure\n            ensure(row)\n", 1)
    source = source.replace(anchors[2],
        "        import shutil\n        if self.config.get('tenant_control'):\n"
        "            from tenant_control_lifecycle import remove\n            remove(row)\n"
        "        source=self.path(row['slug']).resolve();destination=", 1)
    ast.parse(source)
    return source
