import asyncio
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ENTRY=Path(__file__).resolve().parents[1]/'ple_entry'
sys.path.insert(0,str(ENTRY))
spec=importlib.util.spec_from_file_location('pr5_ple_adapter',ENTRY/'pr5_adapter.py')
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
from ple_delivery import Delivery, EvidenceSession, declared_item_updates, declared_package, normalize_spec, first_reads
from container_audit import run as audit_delivery, payload_check


def test_declared_output_table_is_imported_without_task_name_branch():
    source='Write `/filesystem/project/output/submission.zip`.\n| `rows.csv` | `id` |\n| `note.md` | prose |'
    imported=declared_package(source,'/filesystem/project')
    assert imported['archives'][0]['path']=='/filesystem/project/output/submission.zip'
    assert [f['artifact'] for f in imported['files']]==['rows.csv','note.md']
    assert declared_package('no named deliverables','/task') is None


def test_delivery_syntax_checker_does_not_claim_engineering_compliance(tmp_path):
    (tmp_path/'a.csv').write_text('x,y\n1,2\n')
    (tmp_path/'b.json').write_text('{"x":1}')
    spec=normalize_spec({'files':[{'path':'output/a.csv','kind':'text'},
        {'path':'output/b.json','kind':'text'}]},'/task')
    delivery=Delivery(None,spec,[],lambda *a,**k:None)
    result=delivery.local_check(tmp_path)
    assert result['status']=='passed' and 'engineering checked separately' in result['detail']
    (tmp_path/'a.csv').write_text('x,y\n1\n')
    assert delivery.local_check(tmp_path)['status']=='failed'


def test_namespace_translation_leaves_expected_values_and_unknown_paths_unchanged():
    plan={'checks':[{'path':'/task/output/a.json','kind':'json_equals',
        'value':{'path':'/task/output/a.json'},'requirements':['R1']},
        {'path':'/outside/a.json','kind':'file_exists'}],
        'modules':[{'owned_files':['/task/output/a.json']}],
        'root':{'verification':'Read /task/output/a.json'}}
    result=adapter.canonicalize_paths(plan,{'/task/output/a.json':'a.json'},planning=True)
    assert result['checks'][0]['path']=='a.json'
    assert result['checks'][0]['value']=={'path':'/task/output/a.json'}
    assert result['checks'][1]['path']=='/outside/a.json'
    assert result['root']==plan['root']
    assert plan['checks'][0]['path']=='/task/output/a.json'
    with pytest.raises(ValueError):
        adapter.canonicalize_paths({'files':{'a.json':'x','/task/output/a.json':'y'}},
                                   {'/task/output/a.json':'a.json'},planning=False)


def test_transport_never_erases_or_defaults_blocking_questions():
    blocked={'unresolved':['missing evidence'],'checks':[]}
    assert adapter.canonicalize_paths(blocked,{},planning=True)['unresolved']==['missing evidence']
    assert 'unresolved' not in adapter.canonicalize_paths({'checks':[]},{},planning=True)


def test_pr5_native_exchange_preserves_its_request_and_output_contract(tmp_path):
    from feasibility_scheduler.native_client import NativeClient
    from feasibility_scheduler.client import ApiRequest,Outcome
    async def run():
        client=NativeClient(tmp_path,timeout_seconds=5)
        request=ApiRequest(task_id='unit',idempotency_key='real-envelope-test',prompt='fixture',provider=client.provider,model=client.model)
        pending=asyncio.create_task(client.acall(request))
        while not list(tmp_path.glob('*.request.json')): await asyncio.sleep(.01)
        source=next(tmp_path.glob('*.request.json'))
        record=json.loads(source.read_text())
        assert record['task_id']=='unit'
        target=source.with_name(source.name.replace('.request.json','.response.json'))
        target.write_text(json.dumps({'status':'success','producer':'test-only','body':{'ok':True,'files':{'a.csv':'x\n1\n'}}}))
        result=await pending
        assert result.outcome is Outcome.SUCCESS and result.parsed_body['files']['a.csv']=='x\n1\n'
    asyncio.run(run())


class LocalTaskSession:
    """Real subprocess/file operations in an isolated fixture; no model or task data."""
    workdir = '/'
    mcp_servers = []

    def time_left(self): return 21600

    async def read_file(self, path): return Path(path).read_bytes()

    async def write_file(self, path, data, **kwargs):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        return p.stat().st_size

    async def exec(self, command, **kwargs):
        result = await asyncio.to_thread(subprocess.run, command, shell=True, capture_output=True, text=True)
        return {'exit_code': result.returncode, 'output': result.stdout + result.stderr}


def package_spec(root):
    return normalize_spec({'files': [
        {'path': 'output/rows.csv', 'kind': 'text'},
        {'path': 'output/note.md', 'kind': 'text'}],
        'archives': [{'path': 'output/submission.zip', 'members': {
            'rows.csv': 'output/rows.csv', 'note.md': 'output/note.md'}}]}, str(root))


def test_task_import_accepts_binary_and_final_message_without_requiring_a_zip():
    value = normalize_spec({'files': [
        {'path': '/app/files/output/report.docx', 'kind': 'binary'},
        {'path': '/app/files/output/model.ifc', 'kind': 'binary'},
        {'kind': 'final_message'}]}, '/app/files')
    assert len(value['files']) == 3 and value['archives'] == []
    assert value['files'][0]['artifact'].endswith('report.docx.json')
    assert declared_package('Write /app/files/output/report.docx', '/app/files') is None
    with pytest.raises(ValueError):
        normalize_spec({'files': [{'path': '../input.csv', 'kind': 'text'}]}, '/app/files')
    with pytest.raises(ValueError):
        normalize_spec({'files': [{'path': '/app/files/tests/key.json', 'kind': 'text'}]}, '/app/files')


def test_item_zip_check_detects_skipped_intermediate_update(tmp_path):
    from openpyxl import Workbook

    archive_path = tmp_path/'output'/'submission.zip'
    archive_path.parent.mkdir()
    spec = normalize_spec({'files': [{'path': str(archive_path), 'kind': 'binary'}]}, str(tmp_path))
    brief = '''Rewriting the zip after each item you complete is required.
    Study for both circuits (items `C01` … `C02`, fixture).
    Write the ZIP containing `study.xlsx` and the report (`study.md`, `.pdf` or `.docx`).
    Use conveyance types `DB` (site items) and `EMT` (item C02).
    **Items** — `item_id`, `value`
    **Alternatives** — `item_id`, `type`'''
    config = declared_item_updates(brief, spec)
    assert config is not None and config['items'] == {'C01': ['DB'], 'C02': ['EMT']}

    def write_zip(c01, c02, *, c02_clause=None, padded=False):
        book = Workbook()
        book.active.title = 'Items'
        book.active.append(['item_id', 'value'])
        book.active.append(['C01', c01])
        book.active.append(['C02', c02])
        choices = book.create_sheet('Alternatives')
        choices.append(['item_id', 'type', 'governing_clause'])
        choices.append(['C01', 'DB'])
        choices.append(['C02', 'EMT', c02_clause])
        if padded:
            choices['H3'] = '   '
        binary = io.BytesIO()
        book.save(binary)
        book.close()
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr('study.xlsx', binary.getvalue())
            archive.writestr('study.md', 'Fixture report')

    async def run():
        session = EvidenceSession(LocalTaskSession())
        delivery = Delivery(session, spec, [], lambda *a, **k: None, item_config=config)
        delivery.snapshot = None
        write_zip(0, 0, c02_clause='Provisional; not evaluated   ')
        assert (await delivery.checkpoint())['complete']
        write_zip(10, 0, c02_clause='RFP 1.4')
        with pytest.raises(ValueError, match='observed: C01, C02'):
            await delivery.record_item_update('C01')
        write_zip(10, 0, padded=True)
        assert (await delivery.record_item_update('C01'))['sequence'] == 1
        with pytest.raises(ValueError, match='not rewritten'):
            await delivery.record_item_update('C02')
        write_zip(11, 20)
        with pytest.raises(ValueError, match='exactly the named item'):
            await delivery.record_item_update('C02')
        write_zip(10, 20)
        workspace = tmp_path/'workspace'
        receipt = workspace/delivery.names[0]
        receipt.parent.mkdir(parents=True)

        def save_receipt():
            content = archive_path.read_bytes()
            receipt.write_text(json.dumps({'path': str(archive_path), 'size': len(content),
                                          'sha256': hashlib.sha256(content).hexdigest()}))

        save_receipt()
        report, evidence = await delivery.publish(workspace, check_items=True)
        assert report == {'status': 'failed', 'detail': 'Missing recorded ZIP updates for: C02'}
        assert evidence['complete'] and evidence['item_updates']['missing_items'] == ['C02']
        assert (await delivery.record_item_update('C02'))['sequence'] == 2
        assert (await delivery.publish(workspace, check_items=True))[0]['status'] == 'passed'
        write_zip(11, 20)
        save_receipt()
        report, _ = await delivery.publish(workspace, check_items=True)
        assert report['detail'] == 'Unrecorded item changes in the final ZIP: C01'
        await delivery.recover_item_archive()
        restored = await delivery.inspect_item_archive()
        assert restored == delivery.item_latest
        archive_path.write_bytes(b'Unfinished ZIP')
        await delivery.recover_item_archive()
        assert await delivery.inspect_item_archive() == restored

    asyncio.run(run())


@pytest.mark.parametrize('defect', ['stale', 'extra', 'missing', 'duplicate'])
def test_zip_audit_detects_actual_content_and_membership_defects(tmp_path, defect):
    spec = package_spec(tmp_path)
    output = tmp_path/'output'; output.mkdir()
    (output/'rows.csv').write_text('id,value\n1,2\n')
    (output/'note.md').write_text('Current note')
    assert audit_delivery(spec, package=True)['complete']
    archive = output/'submission.zip'
    if defect == 'stale':
        (output/'note.md').write_text('Updated note')
    elif defect == 'missing':
        archive.unlink()
    else:
        with zipfile.ZipFile(archive, 'a') as z:
            z.writestr('extra.txt' if defect == 'extra' else 'note.md', 'unexpected')
    evidence = audit_delivery(spec)
    assert evidence['archives'][0]['status'] == 'failed'
    assert not evidence['complete']


@pytest.mark.parametrize('chunked', [False, True])
def test_first_read_order_and_early_zip_checkpoint_use_executed_evidence(tmp_path, monkeypatch, chunked):
    if chunked:
        monkeypatch.setattr('ple_delivery.SNAPSHOT_CHUNK_BYTES', 128)
        monkeypatch.setattr('ple_delivery.MAX_PARSE_BYTES', 128)

    async def run():
        root = tmp_path/'task'; root.mkdir()
        (root/'task.md').write_text('Read `handover/brief.md` first.')
        (root/'handover').mkdir(); (root/'handover/brief.md').write_text('Issued brief')
        (root/'other.md').write_text('Reference')
        session = EvidenceSession(LocalTaskSession())
        task_path = str(root/'task.md')
        source = (await session.read_file(task_path)).decode()
        priority = first_reads(source, str(root))
        references = await adapter.source_materials(session, str(root), priority)
        assert list(references)[0] == 'provided/handover/brief.md'
        reads_before_snapshot = list(session.reads)
        required = [task_path, *priority]
        assert [r['path'] for r in session.reads][:2] == required
        events = []
        delivery = Delivery(session, package_spec(root), required, lambda name, **fields: events.append(name))
        delivery.snapshot = tmp_path/'first-delivery.zip'
        assert not (await delivery.checkpoint())['complete']
        assert not delivery.snapshot.exists()
        await session.write_file(str(root/'output/rows.csv'), 'id,value\n1,2\n')
        await session.write_file(str(root/'output/note.md'), 'First actual candidate')
        evidence = await delivery.checkpoint()
        assert evidence['complete'] and evidence['required_read_order']
        first = evidence['first_complete_host_operation']
        assert first == session.operations and (root/'output/submission.zip').is_file()
        await session.write_file(str(root/'output/note.md'), 'Improved candidate')
        later = await delivery.checkpoint()
        assert later['first_complete_host_operation'] == first
        with zipfile.ZipFile(root/'output/submission.zip') as z:
            assert z.read('note.md') == b'Improved candidate'
        with zipfile.ZipFile(delivery.snapshot) as saved:
            with zipfile.ZipFile(io.BytesIO(saved.read('submission.zip'))) as original:
                assert original.read('note.md') == b'First actual candidate'
            manifest = json.loads(saved.read('manifest.json'))
            assert manifest['archives'][0]['file'] == 'submission.zip'
        assert events.count('pr5_first_delivery_saved') == 1
        assert session.reads == reads_before_snapshot
        delivery.required_reads = list(reversed(required))
        assert not (await delivery.inspect())['required_read_order']
    asyncio.run(run())


@pytest.mark.parametrize('extension,member', [('xlsx', 'xl/workbook.xml'), ('docx', 'word/document.xml')])
@pytest.mark.parametrize('mode', ['files', 'file'])
def test_binary_receipt_goes_through_original_pr5_delivery_and_detects_later_mutation(tmp_path, extension, member, mode):
    from feasibility_scheduler.adapters.legacy_doc import _ArtifactSink, legacy_artifact_acceptor
    from feasibility_scheduler.models import Task
    from feasibility_scheduler.fake_client import ApiRequest, FakeClient, Step
    async def run():
        root = tmp_path/'task'; output = root/'output'; output.mkdir(parents=True)
        remote = output/('report.' + extension)
        with zipfile.ZipFile(remote, 'w') as z:
            z.writestr(member, '<document/>')
        spec = normalize_spec({'files': [{'path': str(remote), 'kind': 'binary'}]}, str(root))
        session = EvidenceSession(LocalTaskSession())
        delivery = Delivery(session, spec, [], lambda *a, **k: None)
        name = delivery.names[0]
        content = json.dumps({'path': str(remote), 'size': -1, 'sha256': 'invented', 'generation_script': 'fixture generation'})
        body = {'ok': True, 'files': {name: content}} if mode == 'files' else {
            'ok': True, 'delivery': 'file', 'file': {'path': name, 'content': content}}
        body = await delivery.capture_binary(body, [name])
        receipt = json.loads(body['files'][name] if mode == 'files' else body['file']['content'])
        assert receipt['sha256'] != 'invented' and receipt['size'] > 0
        assert receipt['generation_script'] == 'fixture generation'
        workspace = tmp_path/'workspace'; workspace.mkdir()
        sink = _ArtifactSink(workspace)
        acceptor = legacy_artifact_acceptor(sink=sink, mode='loose')
        response = FakeClient([Step.success(json.dumps(body))]).call(ApiRequest(
            task_id='binary', idempotency_key='binary-interface-fixture', prompt='fixture only'))
        task = Task(id='binary', payload={'owned_files': [name]})
        report = acceptor(task, response)
        assert report.passed
        assert (await delivery.publish(workspace))[0]['status'] == 'passed'
        with zipfile.ZipFile(remote, 'a') as z:
            z.writestr('changed.txt', 'a later worker version')
        assert (await delivery.publish(workspace))[0]['status'] == 'failed'
    asyncio.run(run())


def test_unknown_format_is_explicitly_unverified_and_bad_office_file_fails():
    assert payload_check('drawing.ifc', b'plausible-looking text')[0] == 'unverified'
    assert payload_check('report.xlsx', b'not an Excel workbook')[0] == 'failed'


def contract_fixture():
    from test_contract_plan import fixture
    from test_modular_planner import standardize
    return standardize(*fixture())


def test_duplicate_read_paths_are_normalized_before_original_pr5_validation():
    from dataclasses import replace
    from ple_contract import planning_error, prepare_contract

    tasks, plan = contract_fixture()
    tasks = [replace(task, payload={**task.payload, 'external_checker_names': ['package']}) for task in tasks]
    for check_id in ('delivery-package-validation', 'package-execution-evidence'):
        plan['checks'].append({'id': check_id, 'kind': 'external', 'name': 'package',
                               'read_paths': ['/task/output/a.py', 'a.py', 'c.py', 'a.py'],
                               'repair_paths': ['a.py'], 'requirements': ['R1']})
    aliases = {'/task/output/a.py': 'a.py'}
    assert 'read_paths must be a unique relative path list' in planning_error(plan, tasks, 'Compare offers', {})

    class Worker:
        calls = 0

        async def run(self, prompt, **kwargs):
            self.calls += 1
            return plan

    worker = Worker()
    result = asyncio.run(prepare_contract('Native planning prompt', worker=worker, tasks=tasks,
        source='Compare offers', checkers={}, aliases=aliases, canonicalize=adapter.canonicalize_paths,
        event=lambda *a, **k: None))
    assert worker.calls == 1
    assert planning_error(result, tasks, 'Compare offers', {}) is None
    for original, check in zip(plan['checks'][-2:], result['checks'][-2:]):
        assert check == {**original, 'read_paths': ['a.py', 'c.py']}
    assert plan['checks'][-1]['read_paths'] == ['/task/output/a.py', 'a.py', 'c.py', 'a.py']

    result['checks'][-1]['read_paths'] = ['../foreign.py', '../foreign.py']
    rejected = adapter.canonicalize_paths(result, aliases, planning=True)
    assert 'read_paths contains invalid paths' in planning_error(rejected, tasks, 'Compare offers', {})
    result['checks'][-1].update(read_paths=['a.py'], repair_paths=['a.py', 'a.py'])
    unchanged = adapter.canonicalize_paths(result, aliases, planning=True)
    assert 'repair_paths must be a unique relative path list' in planning_error(unchanged, tasks, 'Compare offers', {})


@pytest.mark.parametrize('replan_valid', [True, False])
def test_bad_local_binding_gets_one_complete_replan_with_the_original_prompt(replan_valid):
    import copy
    from ple_contract import planning_error, prepare_contract

    tasks, valid = contract_fixture()
    invalid = copy.deepcopy(valid)
    invalid['modules'][0]['local_checks'] = ['c.py']
    replacement = valid if replan_valid else invalid
    original_prompt = 'Native PR5 prompt with the original task and owned paths.'

    class Worker:
        calls = []

        async def run(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            return invalid if len(self.calls) == 1 else replacement

    worker = Worker()
    result = asyncio.run(prepare_contract(original_prompt, worker=worker, tasks=tasks,
        source='Compare offers', checkers={}, aliases={}, canonicalize=adapter.canonicalize_paths,
        event=lambda *a, **k: None))
    assert len(worker.calls) == 2
    assert worker.calls[0] == (original_prompt, {'planning': True})
    assert worker.calls[1][0].startswith(original_prompt)
    assert 'check c.py is not executable within its local boundary' in worker.calls[1][0]
    assert 'new complete plan' in worker.calls[1][0]
    assert worker.calls[1][1] == {'planning': True}
    assert result == replacement
    assert (planning_error(result, tasks, 'Compare offers', {}) is None) is replan_valid


def test_real_blockers_are_preserved_without_replanning():
    from ple_contract import prepare_contract

    tasks, plan = contract_fixture()
    plan['unresolved'] = ['mandatory input file is unavailable']

    class Worker:
        calls = 0

        async def run(self, prompt, **kwargs):
            self.calls += 1
            return plan

    worker = Worker()
    result = asyncio.run(prepare_contract('Original plan prompt', worker=worker, tasks=tasks,
        source='Compare offers', checkers={}, aliases={}, canonicalize=adapter.canonicalize_paths,
        event=lambda *a, **k: None))
    assert worker.calls == 1 and result == plan


def test_declared_output_list_imports_natural_language_xlsx_and_markdown():
    source = '''
Working in /app/files.
Required deliverables:
- Excel workbook: conveyance_study.xlsx
- Markdown report: conveyance_study_report.md
Write /app/files/output/submission.zip containing both deliverables.
'''
    imported = declared_package(source, '/app/files')
    assert imported is not None
    assert [item['kind'] for item in imported['files']] == ['binary', 'text']
    assert [item['path'] for item in imported['files']] == [
        '/app/files/output/conveyance_study.xlsx',
        '/app/files/output/conveyance_study_report.md',
    ]
    assert imported['archives'][0]['members'] == {
        'conveyance_study.xlsx': '/app/files/output/conveyance_study.xlsx',
        'conveyance_study_report.md': '/app/files/output/conveyance_study_report.md',
    }


def test_declared_output_list_classifies_pdf_and_docx_as_binary():
    source = '''
Deliverables:
1. Cost report: cost_report.pdf
2. Calculation workbook: calculations.xlsx
3. Editable report: editable_report.docx
Create /workspace/output/submission.zip with these files.
'''
    imported = declared_package(source, '/workspace')
    assert imported is not None
    assert [item['kind'] for item in imported['files']] == ['binary', 'binary', 'binary']


def test_task_io_import_retries_empty_files_once_then_accepts_valid_spec():
    class Worker:
        def __init__(self):
            self.calls = []
        async def run(self, prompt, **kwargs):
            self.calls.append((prompt, kwargs))
            if len(self.calls) == 1:
                return {'files': [], 'archives': []}
            return {'files': [{'kind': 'final_message'}], 'archives': []}

    worker = Worker()
    events = []
    result = asyncio.run(adapter.import_task_spec(
        worker, 'Task requires a substantive final response.', '/app/files',
        lambda kind, **values: events.append((kind, values))))
    assert result['files'][0]['kind'] == 'final_message'
    assert len(worker.calls) == 2
    assert worker.calls[1][1] == {'importing': True}
    assert 'previous importer response failed host validation' in worker.calls[1][0]
    assert events[0][0] == 'pr5_task_io_import_invalid'


def test_task_io_import_fails_clearly_after_one_invalid_correction():
    class Worker:
        async def run(self, prompt, **kwargs):
            return {'files': [], 'archives': []}

    events = []
    with pytest.raises(ValueError, match='failed after one correction attempt'):
        asyncio.run(adapter.import_task_spec(
            Worker(), 'Task output instructions are ambiguous.', '/app/files',
            lambda kind, **values: events.append((kind, values))))
    assert [event[0] for event in events] == [
        'pr5_task_io_import_invalid', 'pr5_task_io_import_failed'
    ]


def test_declared_output_list_accepts_explicit_absolute_file_paths():
    source = '''
Required deliverables:
- Workbook: /app/files/output/design.xlsx
- Report: /app/files/output/report.pdf
Package these files in /app/files/output/submission.zip.
'''
    imported = declared_package(source, '/app/files')
    assert imported is not None
    assert [item['path'] for item in imported['files']] == [
        '/app/files/output/design.xlsx',
        '/app/files/output/report.pdf',
    ]


def test_explicit_final_zip_does_not_package_input_or_alternative_formats():
    root = '/filesystem/project'
    archive = root + '/output/submission.zip'
    brief = (f'Read `task.md` first; write your deliverable to `{archive}`.\n'
             'Include `study.xlsx` and a report (`report.md`, `.pdf` or `.docx`).')
    assert declared_package(brief, root) == {
        'root': root,
        'files': [{'path': archive, 'kind': 'binary', 'artifact': 'receipts/submission.zip.json'}],
        'archives': [],
    }


def test_root_review_repairs_declared_outputs_with_pr5(tmp_path, monkeypatch):
    from dataclasses import replace
    from feasibility_scheduler.adapters.legacy_doc import run_legacy_doc
    from feasibility_scheduler.fake_client import FakeClient
    from test_contract_runtime import setup_task, step

    tasks, plan, data, cfg = setup_task(tmp_path)
    data['reference_files'] = {'provided/note.md': 'Keep the source material read-only.'}
    data['external_checker_names'] = ['ple-root-review']
    plan['checks'][-1] = {'id': 'check-totals', 'kind': 'external', 'name': 'ple-root-review',
                          'paths': ['provided/note.md'], 'requirements': ['R1']}
    from ple_contract import bind_root_repairs

    bind_root_repairs(plan, tasks, lambda *a, **k: None)
    effective = plan['checks'][-1]
    assert effective['read_paths'] == ['provided/note.md']
    assert effective['repair_paths'] == ['a.json', 'b.json']
    cfg.task_input_path.write_text(json.dumps(data), encoding='utf-8')
    client = FakeClient([step({'a.json': '{"total":1}'}), step({'b.json': '{"total":2}'}),
                         step({'a.json': '{"total":1}', 'b.json': '{"total":1}'})])
    monkeypatch.setattr('feasibility_scheduler.adapters.legacy_doc.create_client', lambda *a, **k: client)
    reviews = []

    def root_check(workspace):
        totals = [json.loads((workspace/name).read_text())['total'] for name in ('a.json', 'b.json')]
        reviews.append(totals)
        return {'status': 'passed' if totals[0] == totals[1] else 'failed', 'detail': 'Compare totals'}

    result = run_legacy_doc(replace(cfg, contract_repair_limit=5,
                                   external_checkers={'ple-root-review': root_check}))
    assert result.ok
    assert reviews == [[1, 2], [1, 1]]
