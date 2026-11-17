import asyncio
import importlib.util
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
from ple_delivery import Delivery, EvidenceSession, declared_package, normalize_spec, first_reads
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


def test_first_read_order_and_early_zip_checkpoint_use_executed_evidence(tmp_path):
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
        required = [task_path, *priority]
        assert [r['path'] for r in session.reads][:2] == required
        delivery = Delivery(session, package_spec(root), required, lambda *a, **k: None)
        assert not (await delivery.checkpoint())['complete']
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


def test_protocol_feedback_fills_missing_fields_then_passes_original_pr5_validation():
    from ple_contract import preflight, prepare_contract
    import copy
    tasks, valid = contract_fixture()
    original = copy.deepcopy(valid)
    del original['composition']['method']
    del original['composition']['counterexample_review']
    original['modules'][0]['boundary'] = ''
    observations = []
    class Worker:
        calls = 0
        async def run(self, prompt, **kwargs):
            self.calls += 1
            assert all(field in prompt for field in ('composition.method', 'composition.counterexample_review', 'modules[0].boundary'))
            return {'additions':[
                {'path':['composition','method'],'value':valid['composition']['method']},
                {'path':['composition','counterexample_review'],'value':valid['composition']['counterexample_review']},
                {'path':['modules',0,'boundary'],'value':valid['modules'][0]['boundary']}]}
    worker = Worker()
    result = asyncio.run(prepare_contract(original, worker=worker, tasks=tasks,
        source='Compare offers', checkers={}, aliases={}, canonicalize=adapter.canonicalize_paths,
        event=lambda kind, **fields: observations.append((kind, fields))))
    assert worker.calls == 1 and preflight(result, tasks, 'Compare offers', {}) == []
    assert 'method' not in original['composition']  # Host never filled a field itself.
    assert result['checks'] == original['checks']
    assert observations[0][1]['valid'] is False and observations[-1][1]['valid'] is True


def test_real_blockers_do_not_trigger_a_format_request_or_get_cleared():
    from ple_contract import prepare_contract
    tasks, plan = contract_fixture()
    plan['unresolved'] = ['mandatory input file is unavailable']
    plan['composition'] = {}
    class Worker:
        async def run(self, *args, **kwargs): pytest.fail('Must not ask a model to clear a real blocker')
    result = asyncio.run(prepare_contract(plan, worker=Worker(), tasks=tasks,
        source='Compare offers', checkers={}, aliases={}, canonicalize=adapter.canonicalize_paths,
        event=lambda *a, **k: None))
    assert result is plan and result['unresolved'] == ['mandatory input file is unavailable']


@pytest.mark.parametrize('change', ['predicate', 'remove-check', 'dependency'])
def test_protocol_feedback_cannot_weaken_existing_obligations(change):
    from ple_contract import prepare_contract
    import copy
    tasks, plan = contract_fixture()
    del plan['composition']['verification']
    changed = copy.deepcopy(plan)
    changed['composition']['verification'] = 'Recheck outputs'
    if change == 'predicate': changed['checks'][0]['path'] = 'c.py'
    elif change == 'remove-check': changed['checks'].pop()
    else: changed['modules'][0]['execution_dependencies'] = ['b']
    changes={'predicate':(['checks',0,'path'],'c.py'),
             'remove-check':(['checks'],changed['checks']),
             'dependency':(['modules',0,'execution_dependencies'],['b'])}
    path,value=changes[change]
    class Worker:
        async def run(self, *args, **kwargs): return {'additions':[{'path':path,'value':value}]}
    result = asyncio.run(prepare_contract(plan, worker=Worker(), tasks=tasks,
        source='Compare offers', checkers={}, aliases={}, canonicalize=adapter.canonicalize_paths,
        event=lambda *a, **k: None))
    assert result is plan and 'verification' not in result['composition']


def test_malformed_field_types_reach_diagnostics_without_transport_rewriting_them():
    from ple_contract import preflight
    tasks, _ = contract_fixture()
    malformed = {'version': 2, 'unresolved': [], 'checks': None, 'modules': 'wrong type'}
    unchanged = adapter.canonicalize_paths(malformed, {}, planning=True)
    errors = preflight(unchanged, tasks, 'Compare offers', {})
    assert unchanged == malformed
    assert any('checks:' in error for error in errors)
    assert any('composition:' in error for error in errors)


def test_format_correction_cannot_change_a_boolean_expected_value_to_a_number():
    from ple_contract import preserve_obligations
    before={'checks':[{'id':'exact','kind':'json_equals','value':True}]}
    after={'checks':[{'id':'exact','kind':'json_equals','value':1}]}
    with pytest.raises(ValueError,match='predicate'):
        preserve_obligations(before,after)


def test_missing_composition_and_conditional_module_limits_are_collected_together():
    from ple_contract import missing_slots
    tasks,plan=contract_fixture()
    del plan['composition']['counterexample_review']
    for module in plan['modules']:
        del module['acceptance_limitations']
    paths=missing_slots(plan,tasks)
    assert ['composition','counterexample_review'] in paths
    assert all(['modules',i,'acceptance_limitations'] in paths for i in range(len(plan['modules'])))


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
    assert 'previous JSON response was rejected' in worker.calls[1][0]
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
