"""Task I/O adaptation and executable delivery evidence, outside the PR5 core."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import uuid

from container_audit import payload_check

TEXT_SUFFIXES = {'.csv', '.json', '.md', '.txt', '.py', '.xml', '.html', '.yaml', '.yml'}
PROTECTED = {'tests', 'solutions', 'verifier', '.git'}


def clean_path(value, root):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid task path')
    path = PurePosixPath(value)
    if '..' in path.parts or set(path.parts) & PROTECTED:
        raise ValueError('Unsafe task path')
    return str(path if path.is_absolute() else PurePosixPath(root) / path)


def task_root(instruction, workdir):
    found = re.search(r'working in\s+`(/[^`]+)`', instruction, re.I)
    return clean_path(found.group(1) if found else workdir, '/')


def first_reads(instruction, root):
    return list(dict.fromkeys(clean_path(p, root) for p in
        re.findall(r'\bread\s+`([^`]+)`\s+first', instruction, re.I)
        if p != 'task.md'))


def normalize_spec(value, root):
    if not isinstance(value, dict) or not isinstance(value.get('files'), list):
        raise ValueError('Task importer must declare files')
    files, seen, artifacts = [], set(), set()
    for item in value['files']:
        if not isinstance(item, dict) or item.get('kind') not in {'text', 'binary', 'final_message'}:
            raise ValueError('Invalid deliverable declaration')
        kind = item['kind']
        path = clean_path(item['path'], root) if kind != 'final_message' else '@final_message'
        if kind == 'final_message':
            artifact = 'final_message.md'
        else:
            parent = PurePosixPath(root) / 'output'
            p = PurePosixPath(path)
            try:
                artifact = p.relative_to(parent).as_posix()
            except ValueError:
                artifact = 'deliverables/' + path.lstrip('/')
            if kind == 'binary':
                artifact = 'receipts/' + artifact + '.json'
        if path in seen or artifact in artifacts or artifact.startswith('provided/'):
            raise ValueError('Duplicate or conflicting delivery path')
        seen.add(path); artifacts.add(artifact)
        files.append({'path': path, 'kind': kind, 'artifact': artifact})
    archives, archive_paths = [], set()
    for item in value.get('archives', []):
        path = clean_path(item['path'], root)
        members = item.get('members')
        if path in seen or path in archive_paths or not isinstance(members, dict) or not members:
            raise ValueError('Invalid archive declaration')
        cleaned = {}
        for name, source in members.items():
            if not isinstance(name, str) or name.startswith('/') or '..' in PurePosixPath(name).parts or '\\' in name:
                raise ValueError('Unsafe archive member')
            source = clean_path(source, root)
            if source not in seen or source == '@final_message':
                raise ValueError('Archive member is not a declared file')
            cleaned[name] = source
        archive_paths.add(path)
        archives.append({'path': path, 'members': cleaned})
    if not files:
        raise ValueError('Task import requires files or a final-message receipt')
    return {'root': root, 'files': files, 'archives': archives}


OUTPUT_SUFFIXES = {
    '.csv', '.doc', '.docx', '.dwg', '.dxf', '.html', '.ifc', '.jpeg',
    '.jpg', '.json', '.md', '.odp', '.ods', '.odt', '.pdf', '.png',
    '.ppt', '.pptx', '.py', '.rtf', '.tex', '.toml', '.tsv', '.txt',
    '.xls', '.xlsm', '.xlsx', '.xml', '.yaml', '.yml', '.zip',
}
OUTPUT_SECTION = re.compile(
    r'\\b(?:deliverables?|required outputs?|output files?|submission contents?|files to submit)\\b',
    re.I,
)
OUTPUT_ACTION = re.compile(
    r'\\b(?:create|produce|generate|write|save|export|deliver|submit|provide|prepare)\\b',
    re.I,
)
LIST_LINE = re.compile(r'^\\s*(?:[-*+] |\\d+[.)] |\\|)')


def _output_file_mentions(instruction):
    """Extract filenames only from explicit output instructions/lists.

    This is deliberately conservative: references in source-material sections are
    not treated as deliverables merely because they have a familiar extension.
    """
    found = []
    in_output_list = False
    for line in instruction.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            in_output_list = bool(OUTPUT_SECTION.search(stripped))
            continue
        is_output_line = bool(OUTPUT_ACTION.search(stripped) or OUTPUT_SECTION.search(stripped))
        if OUTPUT_SECTION.search(stripped):
            in_output_list = True
        elif in_output_list and not LIST_LINE.match(line) and not is_output_line:
            in_output_list = False
        if not (is_output_line or in_output_list or LIST_LINE.match(line) and in_output_list):
            continue
        # Backticked paths/names are the least ambiguous form.
        candidates = re.findall(r'`([^\\`]+\\.[A-Za-z0-9]+)`', stripped)
        # Also accept plain filenames in bullets such as "Excel workbook: report.xlsx".
        candidates += re.findall(
            r'(?<![A-Za-z0-9_./-])([A-Za-z0-9][A-Za-z0-9_.-]*\\.(?:' +
            '|'.join(sorted((s.lstrip('.') for s in OUTPUT_SUFFIXES), key=len, reverse=True)) +
            r'))\\b',
            stripped,
            re.I,
        )
        for candidate in candidates:
            suffix = PurePosixPath(candidate).suffix.lower()
            if suffix in OUTPUT_SUFFIXES and candidate not in found:
                found.append(candidate)
    return found


def declared_package(instruction, root):
    """Fast-path an explicitly declared ZIP package and its named output files.

    Supports both Markdown output tables and natural-language deliverable lists.
    Ambiguous or incomplete package declarations fall back to the model importer.
    """
    archive_paths = list(dict.fromkeys(re.findall(
        r'(/[\\w./-]+/output/[\\w.-]+\\.zip)\\b', instruction, re.I)))
    names = list(dict.fromkeys(re.findall(
        r'\\|\\s*`([^`]+\\.[a-zA-Z0-9]+)`\\s*\\|', instruction)))
    if not names:
        names = _output_file_mentions(instruction)
    if len(archive_paths) != 1 or not names:
        return None

    archive = clean_path(archive_paths[0], root)
    parent = str(PurePosixPath(archive).parent)
    files = []
    members = {}
    for name in names:
        if PurePosixPath(name).suffix.lower() == '.zip':
            continue
        path = clean_path(name, parent)
        kind = 'text' if PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES else 'binary'
        files.append({'path': path, 'kind': kind})
        member = PurePosixPath(name).name if PurePosixPath(name).is_absolute() else PurePosixPath(name).as_posix()
        if member in members:
            return None
        members[member] = path
    if not files:
        return None
    return normalize_spec({'files': files, 'archives': [{'path': archive, 'members': members}]}, root)


class EvidenceSession:
    """Count host tool operations and explicit reads; retain the official clock."""
    def __init__(self, session):
        self.raw = session
        self.operations = 0
        self.reads = []

    def __getattr__(self, name):
        return getattr(self.raw, name)

    async def read_file(self, path):
        self.operations += 1
        step = self.operations
        data = await self.raw.read_file(path)
        self.reads.append({'path': path, 'operation': step, 'sha256': hashlib.sha256(data).hexdigest()})
        return data

    async def exec(self, command, **kwargs):
        self.operations += 1
        return await self.raw.exec(command, **kwargs)

    async def write_file(self, path, data, **kwargs):
        self.operations += 1
        return await self.raw.write_file(path, data, **kwargs)

    async def http(self, *args, **kwargs):
        self.operations += 1
        return await self.raw.http(*args, **kwargs)


class Delivery:
    def __init__(self, session, spec, required_reads, event):
        self.session, self.spec, self.required_reads, self.event = session, spec, required_reads, event
        self.lock = asyncio.Lock()
        self.script = '/tmp/fbs-delivery-' + uuid.uuid4().hex + '.py'
        self.installed = False
        self.first_complete_operation = None
        self.last_evidence = None

    @property
    def names(self):
        return [f['artifact'] for f in self.spec['files']]

    @property
    def aliases(self):
        return {f['path']: f['artifact'] for f in self.spec['files'] if f['kind'] != 'final_message'}

    async def inspect(self, *, package=False):
        if not self.installed:
            await self.session.write_file(self.script, Path(__file__).with_name('container_audit.py').read_bytes())
            self.installed = True
        result = await self.session.exec('python3 ' + shlex.quote(self.script) + ' ' +
            shlex.quote(json.dumps(self.spec)) + (' --package' if package else ''))
        if result.get('exit_code') != 0:
            raise ValueError('Task-container delivery inspection failed: ' + str(result.get('output', ''))[-1200:])
        evidence = json.loads(result['output'])
        evidence['explicit_input_reads'] = list(self.session.reads)
        observed = [r['path'] for r in self.session.reads]
        # This describes host preloading, not a claim to observe every shell/MCP read.
        evidence['required_read_order'] = observed[:len(self.required_reads)] == self.required_reads
        if evidence['complete'] and self.first_complete_operation is None:
            self.first_complete_operation = self.session.operations
            self.event('pr5_first_complete_delivery',host_operations=self.first_complete_operation,
                       metric='host tool operations, not model turns or official step counts')
        evidence['first_complete_host_operation'] = self.first_complete_operation
        self.last_evidence = evidence
        return evidence

    async def checkpoint(self):
        # A checkpoint packages only files already written by the worker. No placeholders.
        async with self.lock:
            return await self.inspect(package=True)

    async def capture_binary(self, body, owned):
        files = body.get('files')
        single = body.get('file') if body.get('delivery') == 'file' else None
        if isinstance(single, dict) and isinstance(single.get('path'), str):
            files = {single['path']: single.get('content')}
        if not isinstance(files, dict):
            return body
        wanted = [f for f in self.spec['files'] if f['kind'] == 'binary' and f['artifact'] in owned and f['artifact'] in files]
        if not wanted:
            return body
        evidence = await self.checkpoint()
        records = {r['path']: r for r in evidence['files']}
        for item in wanted:
            record = records[item['path']]
            if not record.get('sha256') or not record.get('size'):
                raise ValueError('Binary deliverable is missing: ' + item['path'])
            proposed = json.loads(files[item['artifact']])
            if not isinstance(proposed, dict) or proposed.get('path') != item['path']:
                raise ValueError('Binary receipt path differs from its declared output')
            provenance = {k: proposed[k] for k in ('generation_script', 'reproduce')
                          if isinstance(proposed.get(k), str)}
            # Host observation replaces a self-reported receipt; it never grants acceptance.
            files[item['artifact']] = json.dumps({'path': item['path'], 'size': record['size'],
                'sha256': record['sha256'], 'format_status': record['status'],
                'format_detail': record['detail'], **provenance}, sort_keys=True)
            if isinstance(single, dict) and single['path'] == item['artifact']:
                single['content'] = files[item['artifact']]
        return body

    def local_check(self, workspace):
        failures = []
        for item in self.spec['files']:
            path = workspace / item['artifact']
            if not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
                failures.append(item['artifact'] + ': missing or symlink')
                continue
            if item['kind'] == 'binary':
                try:
                    record = json.loads(path.read_text(encoding='utf8'))
                    if record['path'] != item['path'] or not re.fullmatch('[0-9a-f]{64}', record['sha256']) or record['size'] <= 0:
                        raise ValueError('invalid receipt')
                except (ValueError, KeyError, TypeError):
                    failures.append(item['artifact'] + ': invalid binary receipt')
            else:
                status, detail = payload_check(item['artifact'], path.read_bytes())
                if status == 'failed':
                    failures.append(item['artifact'] + ': ' + detail)
        return {'status': 'failed' if failures else 'passed',
                'detail': '; '.join(failures) if failures else 'Transport syntax only; remote formats and engineering checked separately'}

    async def publish(self, workspace):
        async with self.lock:
            local = self.local_check(workspace)
            if local['status'] != 'passed':
                return local, None
            before = {n: hashlib.sha256((workspace/n).read_bytes()).hexdigest() for n in self.names}
            for item in self.spec['files']:
                if item['kind'] == 'text':
                    await self.session.write_file(item['path'], (workspace/item['artifact']).read_bytes())
            evidence = await self.inspect(package=True)
            records = {r['path']: r for r in evidence['files']}
            failures = []
            for item in self.spec['files']:
                if item['kind'] == 'final_message':
                    continue
                record = records[item['path']]
                expected = (json.loads((workspace/item['artifact']).read_text())['sha256']
                            if item['kind'] == 'binary' else before[item['artifact']])
                if record.get('sha256') != expected or record['status'] == 'failed':
                    failures.append(item['path'] + ': changed, missing or malformed output')
            failures += [r['path'] + ': ' + r['detail'] for r in evidence['archives'] if r['status'] == 'failed']
            if not evidence['required_read_order']:
                failures.append('required initial input read order was not observed')
            if before != {n: hashlib.sha256((workspace/n).read_bytes()).hexdigest() for n in self.names}:
                failures.append('FBS candidate changed while publishing')
            status = 'failed' if failures else 'passed'
            # Format unknowns are explicit and passed to Root for actual parser verification.
            return {'status': status, 'detail': '; '.join(failures) if failures else json.dumps(evidence)}, evidence

    def final_message(self, workspace, accepted):
        messages = [workspace/f['artifact'] for f in self.spec['files'] if f['kind'] == 'final_message']
        text = '\n'.join(p.read_text(encoding='utf8') for p in messages if p.is_file())
        paths = [f['path'] for f in self.spec['files'] if f['kind'] != 'final_message'] + [a['path'] for a in self.spec['archives']]
        return (text + '\n\n' if text else '') + 'PR5 host acceptance=' + str(accepted) + '. Requested paths: ' + ', '.join(paths)
