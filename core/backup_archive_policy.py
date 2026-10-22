"""Validate backup members before any restore writes, without app/DB imports."""
from pathlib import Path,PurePosixPath
import stat
import tarfile
import zipfile


def safe_member(name):
    if not isinstance(name,str) or not name or '\\' in name or '\x00' in name:
        raise ValueError('اسم عضو النسخة غير آمن.')
    path=PurePosixPath(name)
    if path.is_absolute() or any(p=='..' or ':' in p for p in path.parts):
        raise ValueError('مسار عضو النسخة خارج النطاق.')
    return path


def validate_archive(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('ملف النسخة غير صالح.')
    if path.name.endswith('.sql'):return {'sql'}
    seen=set();payload=set()
    def check(name,regular,directory):
        normalized=safe_member(name).as_posix()
        if name.rstrip('/')!=normalized:raise ValueError('مسار غير قياسي في النسخة.')
        if normalized in seen:raise ValueError('أعضاء مكررة في النسخة.')
        seen.add(normalized)
        if not regular and not directory:raise ValueError('الروابط والملفات الخاصة مرفوضة في النسخة.')
        if regular and normalized=='database_dump.sql':payload.add('sql')
        if regular and normalized=='radius_wisp_raw.db':payload.add('sqlite')
    if path.name.endswith(('.tar.gz','.tgz')):
        with tarfile.open(path,'r|gz') as archive:
            for member in archive:
                check(member.name,member.isfile() and not member.issparse(),member.isdir())
    elif path.name.endswith('.zip'):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                mode=stat.S_IFMT(member.external_attr>>16)
                check(member.filename,not member.is_dir() and mode in (0,stat.S_IFREG),member.is_dir() and mode in (0,stat.S_IFDIR))
    else:raise ValueError('صيغة النسخة غير مدعومة.')
    if not payload:raise ValueError('النسخة لا تحتوي قاعدة بيانات قابلة للاستعادة.')
    return payload


def upload_targets(relative,roots):
    relative=safe_member(relative)
    if relative.as_posix()=='.':raise ValueError('مسار ملف فارغ.')
    targets=[]
    for root in roots:
        root=Path(root).resolve();target=root.joinpath(*relative.parts)
        if not target.resolve().is_relative_to(root):raise ValueError('ملف الاستعادة خارج مجلد الرفع.')
        probe=target
        while probe!=root:
            if probe.is_symlink():raise ValueError('رابط في مسار استعادة الملفات.')
            probe=probe.parent
        targets.append(target)
    return targets
