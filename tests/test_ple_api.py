"""API transport probes use synthetic replies, never a benchmark answer."""
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
from time import monotonic
from types import SimpleNamespace
import urllib.error

import httpx2 as httpx
from openai import AsyncOpenAI, OpenAI
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
            assert body['response_format'] == {'type': 'json_object'}
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


def stream_reply(deltas, *, finish='stop'):
    def chunk(choices, **fields):
        return {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                'model': 'fixture-model', 'choices': choices, **fields}

    chunks = [chunk([{'index': 0, 'delta': delta, 'finish_reason': None}]) for delta in deltas]
    if finish:
        chunks.append(chunk([{'index': 0, 'delta': {}, 'finish_reason': finish}]))
        chunks.append(chunk([], usage={'prompt_tokens': 10, 'completion_tokens': 5,
                                      'total_tokens': 15, 'prompt_tokens_details': {'cached_tokens': 8}}))
    sse = ''.join(f'data: {json.dumps(item)}\n\n' for item in chunks)
    if finish:
        sse += 'data: [DONE]\n\n'
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=sse)


def tool_reply():
    return stream_reply([
        {'role': 'assistant', 'tool_calls': [{'index': 0, 'type': 'function', 'id': 'echo-1',
                                             'function': {'name': 'api_echo', 'arguments': '{"value":'}}]},
        {'tool_calls': [{'index': 0, 'function': {'arguments': '"API_READY"}'}}]},
    ], finish='tool_calls')


def test_streaming_probe_roundtrips_split_tool_arguments_and_usage():
    def send(req):
        body = json.loads(req.content)
        assert body['stream'] is True
        assert body['stream_options'] == {'include_usage': True}
        if body.get('tools'):
            assert 'response_format' not in body
            return tool_reply()
        assert body['response_format'] == {'type': 'json_object'}
        assert body['messages'][-1]['tool_call_id'] == 'echo-1'
        assert json.loads(body['messages'][-1]['content']) == {'value': 'API_READY'}
        return stream_reply([{'role': 'assistant', 'content': '{"status":'}, {'content': '"API_READY"}'}])

    with OpenAI(api_key='fixture', base_url=submission().model.endpoint,
                http_client=httpx.Client(transport=httpx.MockTransport(send))) as client:
        result = api.probe(submission(), 'fixture', stream_client=client)
    assert result['ok'] and result['calls'] == 2 and result['tokens'] == {'input': 20, 'output': 10}


@pytest.mark.parametrize('truncated', [False, True])
def test_async_worker_assembles_stream_before_accepting_tools(monkeypatch, capsys, truncated):
    monkeypatch.syspath_prepend(str(PATH.parents[1] / 'ple_entry'))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture-model')
    monkeypatch.setenv('FBS_PLE_MODEL_STREAM', '1')
    from pr5_adapter import ModelWorker

    def send(req):
        assert json.loads(req.content)['stream_options'] == {'include_usage': True}
        if truncated:
            return stream_reply([{'role': 'assistant', 'content': '{"private-output":'}], finish=None)
        return tool_reply()

    async def complete():
        worker = ModelWorker(SimpleNamespace(time_left=lambda: 60))
        await worker.client.close()
        async with AsyncOpenAI(api_key='fixture', base_url=submission().model.endpoint,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(send))) as client:
            worker.client = client
            return await worker.complete('worker', monotonic(), model=worker.model,
                                         messages=[{'role': 'user', 'content': 'Call api_echo'}])

    if truncated:
        with pytest.raises(ValueError, match='ended before the completion finished'):
            asyncio.run(complete())
    else:
        result = asyncio.run(complete())
        call = result.choices[0].message.tool_calls[0]
        assert call.id == 'echo-1' and json.loads(call.function.arguments) == {'value': 'API_READY'}
        assert result.usage.prompt_tokens_details.cached_tokens == 8
    updates = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(updates) == 1 and updates[0]['event'] == 'pr5_model_stream_received'
    assert updates[0].keys() == {'event', 'phase', 'seconds'}


@pytest.mark.parametrize('second_json_valid', [True, False])
def test_planner_replan_consumes_raw_api_text(monkeypatch, capsys, second_json_valid):
    from copy import deepcopy
    from dataclasses import replace
    from test_modular_planner import standardize
    from test_contract_plan import fixture

    monkeypatch.syspath_prepend(str(PATH.parents[1] / 'ple_entry'))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture-model')
    monkeypatch.setenv('FBS_PLE_MODEL_STREAM', '1')
    from ple_contract import prepare_contract
    from pr5_adapter import ModelWorker, canonicalize_paths, event

    tasks, valid = standardize(*fixture())
    profile = {'id': 'fixture', 'categories': [
        {'id': 'delivery', 'description': 'Delivery evidence', 'applicable': True,
         'allowed_kinds': ['file_exists']}]}
    tasks = [replace(task, payload={**task.payload, 'acceptance_profile': profile}) for task in tasks]
    valid['checks'][0]['id'] = 'receipt-present'
    valid['modules'][0]['local_checks'] = ['receipt-present', 'b.py']
    valid['acceptance_assignments'] = [
        {'id': 'own-a', 'category': 'delivery', 'scope': 'local', 'owners': ['a'],
         'requirements': ['R1'], 'checks': ['receipt-present', 'b.py']},
        {'id': 'own-b', 'category': 'delivery', 'scope': 'local', 'owners': ['b'],
         'requirements': ['R1'], 'checks': ['c.py']},
    ]
    incomplete = deepcopy(valid)
    incomplete['acceptance_assignments'][0]['checks'] = ['b.py']
    raw_replies = iter([json.dumps(incomplete), json.dumps(valid) if second_json_valid else '{"private-output":'])
    requests = []

    def send(request):
        requests.append(json.loads(request.content))
        content = next(raw_replies)
        return stream_reply([{'role': 'assistant', 'content': content[:13]}, {'content': content[13:]}])

    async def usage(*args):
        pass

    async def run():
        worker = ModelWorker(SimpleNamespace(time_left=lambda: 60, usage=usage))
        await worker.client.close()
        async with AsyncOpenAI(api_key='fixture', base_url=submission().model.endpoint,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(send))) as client:
            worker.client = client
            return await prepare_contract('Original planner request', worker=worker, tasks=tasks,
                source='Compare offers', checkers={}, aliases={}, canonicalize=canonicalize_paths, event=event)

    if second_json_valid:
        assert asyncio.run(run()) == valid
    else:
        with pytest.raises(json.JSONDecodeError):
            asyncio.run(run())
    log = capsys.readouterr().out
    assert 'unassigned acceptance checks' in log and 'private-output' not in log
    assert ('pr5_planning_json_invalid' in log) is not second_json_valid
    assert len(requests) == 2
    assert all(req['response_format'] == {'type': 'json_object'} for req in requests)
    assert 'Every check ID' in requests[0]['messages'][0]['content']
    assert 'unassigned acceptance checks' in requests[1]['messages'][-1]['content']
