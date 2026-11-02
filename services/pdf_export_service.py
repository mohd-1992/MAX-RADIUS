"""Bounded HTML parts, isolated browser lifetime and complete PDF assembly."""
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from services.heavy_operation_service import operation_slot, temporary_output, ExportCancelled


def group_memory(pgid):
    """Resident memory of this dedicated process group, never other exports."""
    total = 0
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(stat[2]) == pgid:
                total += int((proc / 'statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, ValueError, IndexError):
            pass
    return total


def render_parts(parts, static_folder, timeout=180, memory_mb=768, cancelled=None):
    """Parts are generated lazily; temporary PDFs are merged in order, no truncation."""
    if cancelled is None:
        from services.heavy_operation_service import export_cancelled
        cancelled=export_cancelled
    with operation_slot(), tempfile.TemporaryDirectory(prefix='max-pdf-') as directory:
        root = Path(directory)
        started = time.monotonic()
        try:
            count = 0
            for index, html in enumerate(parts):
                if cancelled and cancelled():
                    raise ExportCancelled('أُلغي التصدير؛ لم يُصدر ملف جزئي.')
                if time.monotonic() - started >= timeout:
                    raise TimeoutError('انتهت مهلة التصدير. حدد نطاقًا أصغر وأعد المحاولة.')
                source = root / 'part.html'; target = root / f'{index}.pdf'
                source.write_text(html, encoding='utf-8')
                command = [sys.executable, '-B', '-c',
                           'from services.pdf_export_service import render_worker; import sys; render_worker(*sys.argv[1:])',
                           str(source), str(target), str(Path(static_folder).resolve())]
                process = subprocess.Popen(command, start_new_session=(os.name != 'nt'),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    while process.poll() is None:
                        if cancelled and cancelled():
                            raise ExportCancelled('أُلغي التصدير؛ لم يُصدر ملف جزئي.')
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError('انتهت مهلة تجهيز الملف؛ أعد المحاولة بنطاق أصغر.')
                        if os.name != 'nt' and group_memory(process.pid) > memory_mb * 1024**2:
                            raise RuntimeError('تعقيد التصميم تجاوز ميزانية التصدير؛ قلل الصور أو حجم النطاق.')
                        time.sleep(.1)
                    if process.returncode or not target.is_file():
                        raise RuntimeError('تعذر تجهيز أحد أجزاء الملف؛ لم يُصدر ملف جزئي.')
                    count += 1
                finally:
                    # The group was created here; it contains only this renderer.
                    if process.poll() is None:
                        # Let the worker unwind Playwright and reap its children
                        # before using a process-group kill as a final fallback.
                        process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name != 'nt':
                            try: os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError: pass
                        else: process.kill()
                        process.wait()
                    if os.name != 'nt':
                        grace=time.monotonic()+2
                        while group_memory(process.pid) and time.monotonic()<grace:time.sleep(.05)
                        if group_memory(process.pid):
                            try:os.killpg(process.pid,signal.SIGKILL)
                            except ProcessLookupError:pass
            # Assembly also runs outside Web: PDF objects must not accumulate
            # in the request worker as a full history grows.
            merged = root / 'complete.pdf'
            process = subprocess.Popen([sys.executable, '-B', '-c',
                'from services.pdf_export_service import merge_worker; import sys; merge_worker(*sys.argv[1:])',
                str(root), str(merged), str(count)], start_new_session=(os.name != 'nt'),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                while process.poll() is None:
                    if cancelled and cancelled():
                        raise ExportCancelled('أُلغي التصدير؛ لم يُصدر ملف جزئي.')
                    if time.monotonic() - started >= timeout:
                        raise TimeoutError('انتهت مهلة تجهيز الملف؛ أعد المحاولة بنطاق أصغر.')
                    if os.name != 'nt' and group_memory(process.pid) > memory_mb * 1024**2:
                        raise RuntimeError('حجم تجميع PDF تجاوز ميزانية التصدير؛ حدد نطاقًا أصغر.')
                    time.sleep(.1)
                if process.returncode or not merged.is_file():
                    raise RuntimeError('تعذر تجميع الملف؛ لم يُصدر ملف جزئي.')
                result = temporary_output()
                try:
                    import shutil
                    with merged.open('rb') as source:
                        shutil.copyfileobj(source, result)
                    result.seek(0)
                    return result
                except BaseException:
                    result.close(); raise
            finally:
                if process.poll() is None:
                    if os.name != 'nt': os.killpg(process.pid, signal.SIGTERM)
                    else: process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        if os.name != 'nt': os.killpg(process.pid, signal.SIGKILL)
                        else: process.kill()
                        process.wait()
        finally:
            close=getattr(parts,'close',None)
            if close:close()


def render_worker(source,target,assets):
    prepare_worker()
    try:
        from services.batch_export_service import _render_pdf_local
        Path(target).write_bytes(_render_pdf_local(Path(source).read_text(encoding='utf-8'), assets))
    finally:
        reap_worker_children()


def prepare_worker():
    if os.name != 'nt':
        import ctypes
        # Adopt only descendants of this isolated renderer, including detached
        # Chromium crash handlers. Never change Web's process-wide child policy.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0):
            raise RuntimeError('تعذر تهيئة عامل التصدير المعزول.')
        def terminate(signum, frame):
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGTERM, terminate)


def reap_worker_children():
    if os.name == 'nt':
        return
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    deadline = time.monotonic() + 3
    sent = set()
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        force = time.monotonic() >= deadline - 1
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                pid = int(proc.name)
                stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(stat[1]) != os.getpid() or stat[0] == 'Z':
                    continue
                action = signal.SIGKILL if force else signal.SIGTERM
                if (pid, action) in sent:
                    continue
                # pidfd pins the process against PID reuse between verification
                # and signalling. The worker owns every adopted child here.
                try:
                    fd = os.pidfd_open(pid)
                except (AttributeError, OSError):
                    # Older supported Linux hosts may lack pidfd. An unreaped
                    # child cannot reuse its PID while this parent is alive.
                    stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                    if int(stat[1]) == os.getpid():
                        os.kill(pid, action)
                        sent.add((pid, action))
                else:
                    try:
                        stat = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                        if int(stat[1]) == os.getpid():
                            signal.pidfd_send_signal(fd, action)
                            sent.add((pid, action))
                    finally:
                        os.close(fd)
            except (OSError, ValueError, IndexError):
                pass
        if time.monotonic() >= deadline:
            raise RuntimeError('تعذر إغلاق عامل التصدير بالكامل.')
        time.sleep(.03)


def merge_worker(directory, target, count):
    from pypdf import PdfWriter
    with PdfWriter() as writer:
        for index in range(int(count)):
            writer.append(str(Path(directory) / f'{index}.pdf'), import_outline=False)
        writer.write(target)


if __name__ == '__main__':
    render_worker(*sys.argv[1:])
