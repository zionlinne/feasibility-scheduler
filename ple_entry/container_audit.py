"""Format and archive checks, executed inside the task container. No grader access."""
from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sys
import xml.etree.ElementTree as ET
import zipfile


MAX_PARSE_BYTES = 32 * 1024 * 1024


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def safe_file(path):
    p = Path(path)
    return p.is_file() and not any(x.is_symlink() for x in (p, *p.parents))


def payload_check(name, data):
    """Structural validity only. A parse never establishes engineering validity."""
    suffix = PurePosixPath(name).suffix.lower()
    if not data:
        return 'failed', 'empty file'
    if suffix in {'.zip', '.xlsx', '.xlsm', '.docx', '.pptx'}:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                if not infos or len(names) != len(set(names)):
                    return 'failed', 'empty archive or duplicate members'
                if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n for n in names):
                    return 'failed', 'unsafe archive member'
                if sum(i.file_size for i in infos) > MAX_PARSE_BYTES:
                    return 'unverified', 'archive expansion exceeds structural audit memory budget'
                if archive.testzip():
                    return 'failed', 'archive checksum failure'
                required = {'.xlsx': 'xl/workbook.xml', '.xlsm': 'xl/workbook.xml',
                            '.docx': 'word/document.xml', '.pptx': 'ppt/presentation.xml'}.get(suffix)
                if required:
                    ET.fromstring(archive.read(required))
                if suffix in {'.xlsx', '.xlsm'}:
                    for n in names:
                        if n.startswith('xl/worksheets/') and n.endswith('.xml'):
                            for cell in ET.fromstring(archive.read(n)).iter():
                                if cell.tag.split('}')[-1] == 'c' and cell.attrib.get('t') == 'e':
                                    return 'failed', 'spreadsheet contains an error cell'
                if suffix == '.zip':
                    unknown = []
                    for info in infos:
                        if info.is_dir():
                            continue
                        if PurePosixPath(info.filename).suffix.lower() == '.zip':
                            unknown.append(info.filename + ': nested archive requires a dedicated check')
                            continue
                        state, detail = payload_check(info.filename, archive.read(info))
                        if state == 'failed':
                            return state, info.filename + ': ' + detail
                        if state == 'unverified':
                            unknown.append(info.filename + ': ' + detail)
                    if unknown:
                        return 'unverified', '; '.join(unknown)
        except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError) as exc:
            return 'failed', type(exc).__name__ + ': invalid archive/document structure'
    elif suffix in {'.txt', '.md', '.json', '.csv', '.py', '.xml', '.html', '.yaml', '.yml'}:
        try:
            text = data.decode('utf-8-sig')
            if not text.strip():
                return 'failed', 'blank text'
            if suffix == '.json':
                json.loads(text)
            elif suffix == '.csv':
                rows = list(csv.reader(io.StringIO(text)))
                if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows[1:]):
                    return 'failed', 'ragged or empty CSV'
            elif suffix == '.py':
                compile(text, name, 'exec')
            elif suffix == '.xml':
                ET.fromstring(text)
            elif suffix in {'.yaml', '.yml', '.html'}:
                return 'unverified', 'decoded text; format needs an appropriate parser'
        except (UnicodeError, ValueError, SyntaxError, ET.ParseError):
            return 'failed', 'invalid text syntax'
    elif suffix == '.pdf':
        if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-2048:]:
            return 'failed', 'invalid PDF envelope'
        return 'unverified', 'PDF envelope checked; rendering/content validation still required'
    elif suffix in {'.ifc', '.dxf', '.dwg', '.png', '.jpg', '.jpeg', '.webp', '.gif'}:
        return 'unverified', 'requires a domain/image parser in Root review'
    else:
        return 'unverified', 'no generic format parser for this extension'
    return 'passed', 'structural parsing only; engineering checked separately'


def inspect_file(path):
    if not safe_file(path):
        return {'path': path, 'status': 'failed', 'detail': 'missing/nonregular/symlink file'}
    p = Path(path)
    size = p.stat().st_size
    if size > MAX_PARSE_BYTES:
        status, detail = 'unverified', 'hashed; format exceeds structural audit memory budget'
    else:
        status, detail = payload_check(path, p.read_bytes())
    return {'path': path, 'size': size, 'sha256': digest(p), 'status': status, 'detail': detail}


def run(spec, *, package=False):
    files = {f['path']: inspect_file(f['path']) for f in spec['files'] if f['kind'] != 'final_message'}
    archives = []
    for item in spec.get('archives', []):
        members = item['members']
        ready = all(safe_file(path) and Path(path).stat().st_size > 0 for path in members.values())
        if package and ready:
            target = Path(item['path'])
            # Never follow a pre-existing output symlink, including a parent.
            if any(p.is_symlink() for p in (target, *target.parents)):
                raise ValueError('unsafe archive destination')
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name('.' + target.name + '.fbs-tmp')
            if temporary.is_symlink():
                raise ValueError('unsafe archive temporary file')
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, path in members.items():
                    archive.write(path, name)
            temporary.replace(target)
        record = inspect_file(item['path'])
        if safe_file(item['path']):
            try:
                with zipfile.ZipFile(item['path']) as archive:
                    names = [i.filename for i in archive.infolist() if not i.is_dir()]
                    if len(names) != len(set(names)) or set(names) != set(members):
                        record.update(status='failed', detail='archive member set differs from declared delivery')
                    elif not ready:
                        record.update(status='failed', detail='archive source file missing')
                    else:
                        hashes = {}
                        for name, path in members.items():
                            value = hashlib.sha256()
                            with archive.open(name) as stream:
                                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                                    value.update(chunk)
                            hashes[name] = value.hexdigest()
                            if hashes[name] != digest(path):
                                record.update(status='failed', detail='stale archive member: ' + name)
                        record['member_sha256'] = hashes
            except (zipfile.BadZipFile, OSError, RuntimeError):
                record.update(status='failed', detail='unreadable archive')
        archives.append(record)
    return {'files': list(files.values()), 'archives': archives,
            'complete': all(f.get('size', 0) > 0 and f['status'] != 'failed'
                            for f in [*files.values(), *archives]) and bool(files or archives)}


if __name__ == '__main__':
    value = json.loads(sys.argv[1])
    print(json.dumps(run(value, package='--package' in sys.argv[2:])))
