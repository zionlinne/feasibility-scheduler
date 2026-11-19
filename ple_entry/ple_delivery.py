"""Task I/O adaptation and executable delivery evidence, outside the PR5 core."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
import uuid
import zipfile

from container_audit import MAX_PARSE_BYTES, payload_check

TEXT_SUFFIXES = {'.csv', '.json', '.md', '.txt', '.py', '.xml', '.html', '.yaml', '.yml'}
PROTECTED = {'tests', 'solutions', 'verifier', '.git'}
FIRST_DELIVERY = Path('/tmp/fbs-first-delivery.zip')
SNAPSHOT_CHUNK_BYTES = 9_000_000
MAX_SNAPSHOT_FILE_BYTES = 95 << 20  # Matches the official PLE export limit.
MAX_SNAPSHOT_BYTES = 256 << 20


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


def declared_item_updates(instruction, spec):
    """Enable the item checkpoint only for an explicit workbook packaging protocol."""
    if not re.search(r'\brewrit(?:e|ing) the zip after each item\b', instruction, re.I):
        return None
    archives = [f['path'] for f in spec['files']
                if f['kind'] == 'binary' and PurePosixPath(f['path']).suffix.lower() == '.zip']
    item_range = re.search(r'\bitems\s+`([A-Z]+)(\d+)`\s*(?:…|\.\.\.|–|to)\s*`\1(\d+)`', instruction)
    workbook = re.search(r'\bcontaining\s+`([^`/\r\n]+\.xlsx)`', instruction)
    report = re.search(r'\(`([^`/\r\n]+)\.md`,\s*`\.pdf`\s+or\s+`\.docx`\)', instruction)
    types = re.search(r'conveyance types\s+([^()]+)\(site items\)\s+and\s+([^()]+)\(item ([A-Z]+\d+)\)', instruction)
    sheets = list(dict.fromkeys(re.findall(r'\*\*([A-Za-z][A-Za-z0-9_]*)\*\*\s+—\s+', instruction)))
    if len(archives) != 1 or not all((item_range, workbook, report, types)) or not {'Items', 'Alternatives'} <= set(sheets):
        return None
    prefix, first, last = item_range.groups()
    numbers = range(int(first), int(last) + 1)
    if not 1 <= len(numbers) <= 100 or len(first) != len(last):
        return None
    items = {prefix + str(n).zfill(len(first)): re.findall(r'`([^`]+)`', types[1]) for n in numbers}
    if types[3] not in items:
        return None
    items[types[3]] = re.findall(r'`([^`]+)`', types[2])
    if not all(items.values()):
        return None
    return {'archive': archives[0], 'workbook': workbook[1], 'sheets': sheets, 'items': items,
            'reports': [report[1] + extension for extension in ('.md', '.pdf', '.docx')]}


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
    r'\b(?:deliverables?|required outputs?|output files?|submission contents?|files to submit)\b',
    re.I,
)
OUTPUT_ACTION = re.compile(
    r'\b(?:create|produce|generate|write|save|export|deliver|submit|provide|prepare|include|attach)\b',
    re.I,
)
LIST_LINE = re.compile(r'^\s*(?:[-*+] |\d+[.)] |\|)')


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
        if not (is_output_line or in_output_list):
            continue
        # Backticked paths/names are the least ambiguous form.
        candidates = re.findall(r'\x60([^\x60]+\.[A-Za-z0-9]+)\x60', stripped)
        # Also accept plain filenames in bullets such as "Excel workbook: report.xlsx".
        candidates += re.findall(
            r'(?<![A-Za-z0-9_-])((?:(?:/[A-Za-z0-9_.-]+)*/)?[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:' +
            '|'.join(sorted((s.lstrip('.') for s in OUTPUT_SUFFIXES), key=len, reverse=True)) +
            r'))\b',
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
        r'(/[\w./-]+/output/[\w.-]+\.zip)\b', instruction, re.I)))
    names = list(dict.fromkeys(re.findall(
        r'\|\s*\x60([^\x60]+\.[a-zA-Z0-9]+)\x60\s*\|', instruction)))
    if len(archive_paths) == 1 and not names:
        archive = archive_paths[0]
        final_deliverables = re.findall(
            r'\bwrite\s+(?:your|the)\s+(?:final\s+)?deliverable\s+to\s+\x60([^\x60]+)\x60',
            instruction, re.I,
        )
        if (set(final_deliverables) == {archive}
                and PurePosixPath(archive).is_relative_to(PurePosixPath(root) / 'output')):
            # The worker owns the whole ZIP when prose permits alternate member formats.
            return normalize_spec({'files': [{'path': archive, 'kind': 'binary'}]}, root)
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
    def __init__(self, session, spec, required_reads, event, item_config=None):
        self.session, self.spec, self.required_reads, self.event = session, spec, required_reads, event
        self.lock = asyncio.Lock()
        self.script = '/tmp/fbs-delivery-' + uuid.uuid4().hex + '.py'
        self.installed = False
        self.first_complete_operation = None
        self.last_evidence = None
        self.snapshot = FIRST_DELIVERY if os.getenv('FBS_PLE_SAVE_FIRST_DELIVERY') == '1' else None
        self.snapshot_saved = False
        self.snapshot_attempt = None
        self.snapshot_attempts = 0
        self.item_config = item_config
        self.item_baseline = None
        self.item_latest = None
        self.item_updates = []
        self.item_snapshot = None

    @property
    def names(self):
        return [f['artifact'] for f in self.spec['files']]

    @property
    def aliases(self):
        return {f['path']: f['artifact'] for f in self.spec['files'] if f['kind'] != 'final_message'}

    async def run_audit(self, data, option=''):
        if not self.installed:
            await self.session.write_file(self.script, Path(__file__).with_name('container_audit.py').read_bytes())
            self.installed = True
        result = await self.session.exec('python3 ' + shlex.quote(self.script) + ' ' +
            shlex.quote(json.dumps(data)) + option)
        if result.get('exit_code') != 0:
            raise ValueError('Task-container delivery inspection failed: ' + str(result.get('output', ''))[-1200:])
        return json.loads(result['output'])

    async def inspect(self, *, package=False):
        evidence = await self.run_audit(self.spec, ' --package' if package else '')
        if self.item_config is not None and self.item_baseline is None and evidence['complete']:
            try:
                baseline = await self.inspect_item_archive()
                archive = next(r for r in evidence['files'] if r['path'] == self.item_config['archive'])
                if baseline['zip_sha256'] != archive['sha256']:
                    raise ValueError('Delivery ZIP changed before the baseline was recorded')
                await self.save_item_version(baseline)
                self.item_baseline = self.item_latest = baseline
            except ValueError as exc:
                evidence['complete'] = False
                evidence['item_baseline_error'] = str(exc)
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
        if self.snapshot is not None and not self.snapshot_saved and evidence['complete']:
            await self.save_first_delivery(evidence)
        return evidence

    async def inspect_item_archive(self):
        observed = await self.run_audit(self.item_config, ' --item-archive')
        if 'error' in observed:
            raise ValueError(observed['error'])
        return observed

    async def save_item_version(self, observed):
        digest = observed['zip_sha256']
        path = self.script + '-latest.zip'
        try:
            await self.run_audit({'source': self.item_config['archive'], 'target': path,
                                  'expected_digest': digest}, ' --copy-zip-version')
            self.item_snapshot = (path, digest)
        except ValueError:
            self.event('pr5_item_version_save_failed')

    async def recover_item_archive(self):
        async with self.lock:
            if self.item_snapshot is None or self.item_latest is None:
                return
            saved, digest = self.item_snapshot
            if digest != self.item_latest['zip_sha256']:
                return
            try:
                await self.inspect_item_archive()
                return  # Keep readable work, even if it has not yet been registered.
            except ValueError:
                pass
            try:
                await self.run_audit({'source': saved, 'target': self.item_config['archive'],
                                      'expected_digest': digest}, ' --copy-zip-version')
                self.event('pr5_item_version_restored')
            except ValueError:
                self.event('pr5_item_version_restore_failed')

    async def record_item_update(self, item_id):
        if self.item_config is None or not isinstance(item_id, str) or item_id not in self.item_config['items']:
            raise ValueError('Unknown delivery item ID')
        async with self.lock:
            if self.item_latest is None:
                raise ValueError('Write the complete provisional ZIP before recording any completed item')
            observed = await self.inspect_item_archive()
            if observed['zip_sha256'] == self.item_latest['zip_sha256']:
                raise ValueError('The ZIP was not rewritten since the last recorded version')
            changed = {item for item, version in observed['item_sha256'].items()
                       if version != self.item_latest['item_sha256'][item]}
            if changed != {item_id}:
                raise ValueError('A checkpoint must contain changes to exactly the named item; observed: '
                                 + ', '.join(sorted(changed)))
            record = {'item': item_id, 'sequence': len(self.item_updates) + 1,
                      'previous_zip_sha256': self.item_latest['zip_sha256'],
                      'zip_sha256': observed['zip_sha256'], 'item_sha256': observed['item_sha256'][item_id]}
            await self.save_item_version(observed)
            self.item_latest = observed
            self.item_updates.append(record)
            self.event('pr5_item_zip_recorded', **record,
                       completed=len({r['item'] for r in self.item_updates}), required=len(self.item_config['items']))
            return record

    async def check_item_updates(self, evidence):
        missing = set(self.item_config['items']) - {r['item'] for r in self.item_updates}
        summary = {'baseline_zip_sha256': self.item_baseline['zip_sha256'] if self.item_baseline else None,
                   'records': list(self.item_updates), 'missing_items': sorted(missing)}
        evidence['item_updates'] = summary
        if missing:
            return 'Missing recorded ZIP updates for: ' + ', '.join(sorted(missing))
        candidate = next(r for r in evidence['files'] if r['path'] == self.item_config['archive'])
        if candidate.get('sha256') != self.item_latest['zip_sha256']:
            try:
                observed = await self.inspect_item_archive()
            except ValueError as exc:
                return str(exc)
            if observed['zip_sha256'] != candidate.get('sha256'):
                return 'Delivery ZIP changed during final inspection'
            changed = sorted(item for item, version in observed['item_sha256'].items()
                             if version != self.item_latest['item_sha256'][item])
            if changed:
                return 'Unrecorded item changes in the final ZIP: ' + ', '.join(changed)
        return None

    async def save_first_delivery(self, evidence):
        files = [r for r in [*evidence['files'], *evidence['archives']]
                 if PurePosixPath(r['path']).suffix.lower() == '.zip']
        if not files:
            return
        signature = tuple(r['sha256'] for r in files)
        if signature != self.snapshot_attempt:
            self.snapshot_attempt = signature
            self.snapshot_attempts = 0
        if self.snapshot_attempts >= 2:
            return
        self.snapshot_attempts += 1
        remote = self.session.raw
        try:
            if sum(r['size'] for r in files) > MAX_SNAPSHOT_BYTES:
                raise ValueError('ZIPs exceed the official task export size limit')
            with tempfile.TemporaryDirectory(dir=self.snapshot.parent) as directory:
                bundle = Path(directory) / 'first-delivery.zip'
                baseline = []
                with zipfile.ZipFile(bundle, 'w', compression=zipfile.ZIP_STORED) as archive:
                    for index, record in enumerate(files, start=1):
                        name = PurePosixPath(record['path']).name
                        member = f'{index:02d}/{name}' if len(files) > 1 else name
                        await self.write_snapshot_member(remote, archive, member, record)
                        baseline.append({'file': member, 'sha256': record['sha256'], 'bytes': record['size']})
                    archive.writestr('manifest.json', json.dumps({'archives': baseline}, indent=2) + '\n')
                bundle.replace(self.snapshot)
            self.snapshot_saved = True
            self.event('pr5_first_delivery_saved')
        except Exception as exc:
            self.event('pr5_first_delivery_save_failed', error_type=type(exc).__name__)

    async def write_snapshot_member(self, remote, archive, member, record):
        path, size = record['path'], record['size']
        if size > MAX_SNAPSHOT_FILE_BYTES:
            raise ValueError('ZIP exceeds the official export size limit')
        digest = hashlib.sha256()
        total = 0
        chunks = [path]
        directory = None
        try:
            if size > SNAPSHOT_CHUNK_BYTES:
                directory = '/tmp/fbs-snapshot-' + uuid.uuid4().hex
                prefix = directory + '/chunk-'
                command = (f'mkdir -m 700 -- {directory} && split -b {SNAPSHOT_CHUNK_BYTES} -d -a 3 '
                           f'-- {shlex.quote(path)} {prefix}')
                result = await remote.exec(command)
                if result.get('exit_code') != 0:
                    raise ValueError('Could not stage ZIP chunks')
                chunks = [f'{prefix}{index:03d}' for index in range((size + SNAPSHOT_CHUNK_BYTES - 1)
                                                                     // SNAPSHOT_CHUNK_BYTES)]
            with archive.open(member, 'w') as output:
                for chunk in chunks:
                    data = await remote.read_file(chunk)
                    total += len(data)
                    digest.update(data)
                    output.write(data)
            if total != size or digest.hexdigest() != record['sha256']:
                raise ValueError('ZIP changed while saving the checkpoint')
            if size > MAX_PARSE_BYTES:
                # The delivery audit deliberately skips parsing ZIP bodies over its memory budget.
                with archive.open(member) as copy, zipfile.ZipFile(copy) as delivery_zip:
                    names = delivery_zip.namelist()
                    if not names or len(names) != len(set(names)) or any(
                            PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name
                            for name in names):
                        raise ValueError('Invalid ZIP members')
        finally:
            if directory is not None:
                await remote.exec(f'rm -rf -- {directory}')

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

    async def publish(self, workspace, *, check_items=False):
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
            if check_items and self.item_config is not None and not failures:
                error = await self.check_item_updates(evidence)
                if error:
                    failures.append(error)
            status = 'failed' if failures else 'passed'
            # Format unknowns are explicit and passed to Root for actual parser verification.
            return {'status': status, 'detail': '; '.join(failures) if failures else json.dumps(evidence)}, evidence

    def final_message(self, workspace, accepted):
        messages = [workspace/f['artifact'] for f in self.spec['files'] if f['kind'] == 'final_message']
        text = '\n'.join(p.read_text(encoding='utf8') for p in messages if p.is_file())
        paths = [f['path'] for f in self.spec['files'] if f['kind'] != 'final_message'] + [a['path'] for a in self.spec['archives']]
        return (text + '\n\n' if text else '') + 'PR5 host acceptance=' + str(accepted) + '. Requested paths: ' + ', '.join(paths)
