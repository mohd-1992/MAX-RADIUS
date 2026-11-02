"""Split extended INSERT lines into bounded batches without splitting quoted data."""
import re


def schema_headers(stream):
    """DDL metadata only; discard huge INSERT lines in 64 KiB pieces."""
    beginning=True
    while True:
        chunk=stream.readline(65536)
        if not chunk:return
        if beginning and re.match(r'^\s*(?:/\*![0-9]+\s+)?CREATE\s+TABLE\b',chunk,re.I):
            yield chunk
        beginning=chunk.endswith('\n')


def bounded_sql_lines(stream, max_bytes=2*1024**3, max_row=1024**2, batch_size=256):
    used=0
    while True:
        first=stream.readline(65536)
        if not first: return
        used+=len(first.encode('utf-8'))
        if used>max_bytes: raise ValueError('ظ…ظ„ظپ SQL ظٹطھط¬ط§ظˆط² 2 GiB ط¨ط¹ط¯ ظپظƒ ط§ظ„ط¶ط؛ط·.')
        if not first.lstrip().startswith('INSERT INTO `'):
            if len(first)==65536 and not first.endswith('\n'):
                raise ValueError('ط³ط·ط± SQL ط؛ظٹط± ظ…ط¯ط¹ظˆظ… ظٹطھط¬ط§ظˆط² 64 KiB.')
            yield first
            continue
        match=re.match(r'\s*INSERT INTO `([^`]+)`\s+VALUES\s*\(',first)
        if not match: raise ValueError('طµظٹط؛ط© INSERT ط؛ظٹط± ظ…ط¯ط¹ظˆظ…ط©ط› ظٹظ„ط²ظ… طھطµط¯ظٹط± SQL ط¨ط¹ط¨ط§ط±ط§طھ VALUES.')
        prefix='INSERT INTO `'+match[1]+'` VALUES '; chunk=first[match.end()-1:]
        row=[]; batch=[]; batch_bytes=0; depth=0; quote=''; escaped=False; ended=False
        while True:
            for char in chunk:
                if quote:
                    row.append(char)
                    if escaped: escaped=False
                    elif char=='\\': escaped=True
                    elif char==quote: quote=''
                elif char in ("'",'"'):
                    quote=char; row.append(char)
                elif char=='(':
                    depth+=1; row.append(char)
                elif char==')':
                    depth-=1; row.append(char)
                    if depth<0: raise ValueError('ط£ظ‚ظˆط§ط³ SQL ط؛ظٹط± ظ…طھط·ط§ط¨ظ‚ط©.')
                    if depth==0:
                        value=''.join(row);row=[]
                        batch.append(value);batch_bytes+=len(value.encode('utf-8'))
                        if len(batch)==batch_size or batch_bytes>=4*1024**2:
                            yield prefix+','.join(batch)+';\n';batch=[];batch_bytes=0
                elif depth:
                    row.append(char)
                elif char==';': ended=True
                elif char not in ',\r\n\t ':
                    raise ValueError('ظ†ظ‡ط§ظٹط© INSERT ط؛ظٹط± ظ…ط¯ط¹ظˆظ…ط©ط› ظ„ظ… طھظڈطھط¬ط§ظ‡ظ„ ط¨ظٹط§ظ†ط§طھ SQL.')
                if len(row)>max_row: raise ValueError('ط³ط¬ظ„ SQL ط§ظ„ظˆط§ط­ط¯ ظٹطھط¬ط§ظˆط² 1 MiBط› ظ„ظ… ظٹط³طھظˆط±ط¯ ط§ظ„ظ…ظ„ظپ.')
            if ended:
                if depth or quote: raise ValueError('ط¹ط¨ط§ط±ط© SQL ط؛ظٹط± ظ…ظƒطھظ…ظ„ط©.')
                if batch: yield prefix+','.join(batch)+';\n'
                break
            chunk=stream.readline(65536)
            if not chunk: raise ValueError('ظ…ظ„ظپ SQL ظٹظ†طھظ‡ظٹ ط¨ط¹ط¨ط§ط±ط© ط؛ظٹط± ظ…ظƒطھظ…ظ„ط©.')
            used+=len(chunk.encode('utf-8'))
            if used>max_bytes: raise ValueError('ظ…ظ„ظپ SQL ظٹطھط¬ط§ظˆط² 2 GiB ط¨ط¹ط¯ ظپظƒ ط§ظ„ط¶ط؛ط·.')


def split_insert_rows(raw):
    """The legacy consumer receives row contents without outer parentheses."""
    rows=[]; start=0; quote=''; escaped=False; depth=0; i=0
    while i<len(raw):
        char=raw[i]
        if quote:
            if escaped: escaped=False
            elif char==chr(92): escaped=True
            elif char==quote: quote=''
        elif char in ("'",'"'): quote=char
        elif char=='(': depth+=1
        elif char==')':
            if depth: depth-=1
            elif raw[i:i+3]=='),(':
                rows.append(raw[start:i]);start=i+3;i+=2
        i+=1
    rows.append(raw[start:])
    return rows
