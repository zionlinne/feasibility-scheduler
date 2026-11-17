"""API transport probes use synthetic replies, never a benchmark answer."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

PATH=Path(__file__).resolve().parents[1]/'cloud/check_api.py'
spec=importlib.util.spec_from_file_location('api_probe_fixture',PATH)
api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)


def submission():
    return SimpleNamespace(model=SimpleNamespace(name='fixture-model',endpoint='https://fixture.invalid/v1'))


def test_probe_sends_actual_model_and_roundtrips_tool_result():
    requests=[]
    def send(req,timeout):
        body=json.loads(req.data);requests.append(body)
        assert req.full_url=='https://fixture.invalid/v1/chat/completions'
        assert req.headers['Authorization']=='Bearer private-fixture-key'
        if len(requests)==1:
            message={'role':'assistant','content':None,'tool_calls':[
                {'id':'echo-1','type':'function','function':{'name':'api_echo','arguments':'{"value":"API_READY"}'}}]}
        else:
            assert body['messages'][-1]['tool_call_id']=='echo-1'
            assert json.loads(body['messages'][-1]['content'])=={'value':'API_READY'}
            message={'role':'assistant','content':'{"status":"API_READY"}'}
        return io.BytesIO(json.dumps({'choices':[{'message':message}],
            'usage':{'prompt_tokens':10,'completion_tokens':5}}).encode())
    result=api.probe(submission(),'private-fixture-key',request=send)
    assert result['ok'] and result['calls']==2 and result['tokens']=={'input':20,'output':10}
    assert all(r['model']=='fixture-model' and r['reasoning_effort']=='high' for r in requests)


def test_probe_rejects_text_instead_of_tool_call():
    def send(*args,**kwargs):
        return io.BytesIO(b'{"choices":[{"message":{"content":"API_READY"}}]}')
    with pytest.raises(ValueError,match='required tool call'):
        api.probe(submission(),'private-fixture-key',request=send)


def test_probe_error_output_does_not_leak_provider_body(monkeypatch,capsys):
    monkeypatch.setattr('sys.argv',['check_api.py','fixture'])
    monkeypatch.setattr(api,'settings',lambda _:submission())
    def refused(*args,**kwargs):
        raise urllib.error.HTTPError('https://fixture.invalid/private-token',401,
                                    'private-provider-body',{},None)
    monkeypatch.setattr(api,'probe',refused)
    assert api.main()==1
    assert json.loads(capsys.readouterr().out)=={'ok':False,'error_type':'HTTPError','http_status':401}


def test_probe_rejects_multiline_key_without_connecting():
    def forbidden(*args,**kwargs): raise AssertionError('Must not connect')
    with pytest.raises(ValueError,match='missing or invalid'):
        api.probe(submission(),'key\nOTHER=bad',request=forbidden)
