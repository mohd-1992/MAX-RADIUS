"""Build proprietary modules without changing Python annotation semantics."""
from pathlib import Path
import sysconfig

from Cython.Build import cythonize
from setuptools import Extension, setup

root = Path('/app')
sources = sorted(p for p in root.rglob('*.py')
                 if not {'build', 'storage', 'uploads', '__pycache__'}.intersection(p.relative_to(root).parts))
extensions, packages = [], []
for path in sources:
    relative = path.relative_to(root)
    name = '.'.join(relative.with_suffix('').parts)
    if path.name == '__init__.py':
        name = '.'.join(relative.parent.parts)
        packages.append((name, path.parent))
    extensions.append(Extension(name, [str(path)], extra_compile_args=['-O1']))

setup(name='MAXProtectedRuntime', ext_modules=cythonize(
    extensions, nthreads=1, compiler_directives={
        'language_level': 3, 'annotation_typing': False, 'infer_types': False,
        'binding': True, 'always_allow_keywords': True, 'overflowcheck': True,
        'boundscheck': True, 'wraparound': True,
    }), script_args=['build_ext', '--inplace', '--parallel', '1'])
suffix = sysconfig.get_config_var('EXT_SUFFIX')
for name, folder in packages:
    (root / (name.replace('.', '/') + suffix)).replace(folder / ('__init__' + suffix))

# All deletion is confined to disposable /app in the build stage.
import shutil
shutil.rmtree(root / 'build', ignore_errors=True)
for path in root.rglob('*'):
    if path.is_file() and path.suffix in {'.py', '.pyc', '.pyo', '.c'}:
        path.unlink()
assert not list(root.rglob('*.py'))
