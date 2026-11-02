"""Rebind dump DDL owners without changing SQL data; stage before any import.

The caller supplies binary streams. No application/config imports or DB writes.
Executable dump comments and client DELIMITER directives retain their spelling.
"""
import io
import re


MAX_TOKEN = 16 * 1024 * 1024
MAX_INPUT = 2 * 1024 * 1024 * 1024
_TOKEN = re.compile(
    rb"(?P<quoted>'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:\\.|``|[^`\\])*`)"
    rb"|(?P<comment>/\*.*?\*/|--(?=[\x00-\x20])[^\n]*|\#[^\n]*)"
    rb"|(?P<word>[A-Za-z0-9_$]+)|(?P<space>\s+)|(?P<symbol>.)", re.S)
_DIRECTIVE = re.compile(rb'[ \t]*DELIMITER[ \t]+(\S+)[ \t]*(?:\r?\n|$)', re.I)
_EXEC = re.compile(rb'(/\*(?:!|M!)[0-9]*\s*)(.*)(\*/)$', re.S)
_KINDS = {b'VIEW', b'TRIGGER', b'FUNCTION', b'PROCEDURE', b'EVENT'}
_OTHER_KINDS = {b'TABLE', b'DATABASE', b'SCHEMA', b'INDEX', b'USER', b'ROLE',
                b'SERVER', b'TABLESPACE', b'SEQUENCE'}
_INSERT_SCAN = re.compile(
    rb"(?P<quoted>'(?:\\.|''|[^'\\])*'|\"(?:\\.|\"\"|[^\"\\])*\"|`(?:\\.|``|[^`\\])*`)"
    rb"|(?P<comment>/\*|--(?=[\x00-\x20])|\#)|(?P<bad>['\"`])|(?P<end>;)", re.S)


def _single_insert(data):
    """Fast-copy a complete native INSERT only after quote-aware end checking."""
    if not data.lstrip().startswith(b'INSERT INTO ') or not data.rstrip().endswith(b';'):
        return False
    end = len(data.rstrip())
    found = False
    for match in _INSERT_SCAN.finditer(data):
        if match.lastgroup in ('comment', 'bad'):
            return False
        if match.lastgroup == 'end':
            if found or match.end() != end:
                return False
            found = True
    return found


class _State:
    def __init__(self):
        self.delimiter = b';'
        self.first = True
        self.header = False
        self.clause = None
        self.rebound_header = False
        self.rebound = 0
        self.is_set = False
        self.last_word = None

    def end(self):
        if self.clause not in (None, 'current', 'current_done') or self.rebound_header:
            raise ValueError('Incomplete or unsupported SQL DEFINER declaration.')
        self.first = True
        self.header = False
        self.clause = None
        self.is_set = False
        self.last_word = None


def _tokens(stream, state, budget):
    while True:
        data = stream.readline(MAX_TOKEN + 1)
        if not data:
            return
        budget[0] += len(data)
        if len(data) > MAX_TOKEN or budget[0] > MAX_INPUT:
            raise ValueError('SQL restore input exceeds its bounded parsing limit.')
        if budget[0] == len(data) and data.startswith(b'\xef\xbb\xbf'):
            yield 'comment', data[:3], None
            data = data[3:]
        directive = _DIRECTIVE.fullmatch(data)
        if directive:
            value = directive[1]
            if len(value) > 16 or any(x in value for x in (b"'", b'"', b'`', b'\\')):
                raise ValueError('Unsupported SQL DELIMITER.')
            yield 'directive', data, value
            continue
        if state.first and state.delimiter == b';' and _single_insert(data):
            yield 'copy', data, None
            continue
        pos = 0
        while pos < len(data):
            if data.startswith(state.delimiter, pos):
                yield 'end', state.delimiter, None
                pos += len(state.delimiter)
                continue
            match = _TOKEN.match(data, pos)
            kind, raw = match.lastgroup, match[0]
            if ((data[pos:pos + 1] in (b"'", b'"', b'`') and kind != 'quoted')
                    or (data.startswith(b'/*', pos) and kind != 'comment')):
                # A token can span physical lines. Retain only its bounded tail.
                tail = data[pos:]
                extra = stream.readline(MAX_TOKEN + 1)
                if not extra:
                    raise ValueError('Unterminated SQL quote or comment.')
                budget[0] += len(extra)
                if len(tail) + len(extra) > MAX_TOKEN or budget[0] > MAX_INPUT:
                    raise ValueError('SQL restore token exceeds its bounded parsing limit.')
                data, pos = tail + extra, 0
                continue
            if kind == 'word' and state.delimiter != b';':
                # END$$ is a word to the generic lexer but a client delimiter.
                index = raw.find(state.delimiter)
                if index > 0:
                    raw = raw[:index]
            yield kind, raw, None
            pos += len(raw)


def _rewrite(stream, output, state, budget):
    for kind, raw, value in _tokens(stream, state, budget):
        if kind == 'copy':
            state.end()
            output.write(raw)
            continue
        if kind == 'comment':
            executable = _EXEC.fullmatch(raw)
            if executable and not raw.startswith(b'/*M!999999'):
                output.write(executable[1])
                # Content was already included in the outer input budget.
                _rewrite(io.BytesIO(executable[2]), output, state, [-len(executable[2])])
                output.write(executable[3])
            else:
                output.write(raw)
            continue
        if kind == 'space':
            output.write(raw)
            continue
        if kind == 'directive':
            if state.clause not in (None, 'current', 'current_done'):
                raise ValueError('DELIMITER inside a SQL DEFINER declaration.')
            state.delimiter = value
            output.write(raw)
            continue
        if kind == 'end':
            state.end()
            output.write(raw)
            continue
        upper = raw.upper() if kind == 'word' else None
        if state.is_set and kind == 'quoted' and b'NO_BACKSLASH_ESCAPES' in raw.upper():
            raise ValueError('NO_BACKSLASH_ESCAPES dumps require an explicit compatible export.')
        clause = state.clause
        if clause == 'equal':
            if raw != b'=':
                raise ValueError('Unsupported SQL DEFINER declaration.')
            output.write(raw)
            state.clause = 'owner'
            continue
        if clause == 'owner':
            if kind not in ('quoted', 'word') or len(raw) > 1024:
                raise ValueError('Unsupported SQL DEFINER account.')
            if upper == b'CURRENT_USER':
                output.write(raw)
                state.clause = 'current'
            else:
                output.write(b'CURRENT_USER')
                state.clause = 'at'
            continue
        if clause == 'at':
            if raw != b'@':
                raise ValueError('Missing SQL DEFINER host.')
            state.clause = 'host'
            continue
        if clause == 'host':
            if kind not in ('quoted', 'word') or len(raw) > 1024:
                raise ValueError('Unsupported SQL DEFINER host.')
            state.clause = None
            state.rebound += 1
            state.rebound_header = True
            continue
        if clause == 'current':
            if raw == b'(':
                output.write(raw)
                state.clause = 'current_close'
                continue
            state.clause = None
        elif clause == 'current_close':
            if raw != b')':
                raise ValueError('Invalid CURRENT_USER in SQL DEFINER.')
            output.write(raw)
            state.clause = 'current_done'
            continue
        elif clause == 'current_done':
            state.clause = None
        if state.first:
            state.first = False
            state.header = upper in (b'CREATE', b'ALTER')
            state.is_set = upper == b'SET'
        elif state.header and upper == b'DEFINER' and state.last_word != b'SECURITY':
            state.clause = 'equal'
            state.rebound_header = True
        elif state.header and upper in _KINDS | _OTHER_KINDS:
            if state.rebound_header and upper not in _KINDS:
                raise ValueError('DEFINER is unsupported for this SQL object.')
            state.rebound_header = False
            state.header = False
        if upper is not None:
            state.last_word = upper
        output.write(raw)


def rebind_dump_definers(source, destination):
    """Write a fully preflighted copy; caller must not import a partial output."""
    state = _State()
    _rewrite(source, destination, state, [0])
    if state.clause not in (None, 'current', 'current_done') or state.rebound_header:
        raise ValueError('Incomplete SQL DEFINER declaration.')
    return {'definers_rebound': state.rebound}
