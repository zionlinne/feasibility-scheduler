import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ENTRY=Path(__file__).resolve().parents[1]/'ple_entry'
sys.path.insert(0,str(ENTRY))
spec=importlib.util.spec_from_file_location('pr5_ple_adapter',ENTRY/'pr5_adapter.py')
adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)


def test_declared_output_table_is_imported_without_task_name_branch():
    source='Write `/filesystem/project/output/submission.zip`.\n| `rows.csv` | `id` |\n| `note.md` | prose |'
    assert adapter.output_spec(source)==('/filesystem/project/output/submission.zip',['rows.csv','note.md'])
    with pytest.raises(ValueError):adapter.output_spec('no named deliverables')


def test_delivery_syntax_checker_does_not_claim_engineering_compliance(tmp_path):
    (tmp_path/'a.csv').write_text('x,y\n1,2\n')
    (tmp_path/'b.json').write_text('{"x":1}')
    result=adapter.format_check(tmp_path,['a.csv','b.json'])
    assert result['status']=='passed' and 'engineering checked separately' in result['detail']
    (tmp_path/'a.csv').write_text('x,y\n1\n')
    assert adapter.format_check(tmp_path,['a.csv','b.json'])['status']=='failed'


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
