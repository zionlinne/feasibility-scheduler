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
from openai import APIConnectionError, AsyncOpenAI, OpenAI
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
            assert 'json' in body['messages'][0]['content'].lower()
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
            return tool_reply()
        assert body['response_format'] == {'type': 'json_object'}
        assert 'json' in body['messages'][0]['content'].lower()
        assert body['messages'][-1]['tool_call_id'] == 'echo-1'
        assert json.loads(body['messages'][-1]['content']) == {'value': 'API_READY'}
        return stream_reply([{'role': 'assistant', 'content': '{"status":'}, {'content': '"API_READY"}'}])

    with OpenAI(api_key='fixture', base_url=submission().model.endpoint,
                http_client=httpx.Client(transport=httpx.MockTransport(send))) as client:
        result = api.probe(submission(), 'fixture', stream_client=client)
    assert result['ok'] and result['calls'] == 2 and result['tokens'] == {'input': 20, 'output': 10}


@pytest.mark.parametrize('mode', ['complete', 'truncated', 'early_disconnect', 'late_disconnect'])
def test_async_worker_assembles_stream_before_accepting_tools(monkeypatch, capsys, mode):
    monkeypatch.syspath_prepend(str(PATH.parents[1] / 'ple_entry'))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture-model')
    monkeypatch.setenv('FBS_PLE_MODEL_STREAM', '1')
    from pr5_adapter import ModelStreamInterrupted, ModelWorker

    partial = stream_reply([{'role': 'assistant', 'content': '{"private-output":'}], finish=None)

    class Disconnect(httpx.AsyncByteStream):
        async def __aiter__(self):
            if mode == 'late_disconnect':
                yield partial.content
            raise httpx.ReadError('fixture disconnected')

    def send(req):
        assert json.loads(req.content)['stream_options'] == {'include_usage': True}
        if mode.endswith('disconnect'):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Disconnect())
        return partial if mode == 'truncated' else tool_reply()

    async def complete():
        worker = ModelWorker(SimpleNamespace(time_left=lambda: 60))
        await worker.client.close()
        async with AsyncOpenAI(api_key='fixture', base_url=submission().model.endpoint,
                               http_client=httpx.AsyncClient(transport=httpx.MockTransport(send))) as client:
            worker.client = client
            return await worker.complete('worker', monotonic(), model=worker.model,
                                         messages=[{'role': 'user', 'content': 'Call api_echo'}])

    if mode == 'early_disconnect':
        with pytest.raises(APIConnectionError):
            asyncio.run(complete())
    elif mode != 'complete':
        with pytest.raises(ModelStreamInterrupted):
            asyncio.run(complete())
    else:
        result = asyncio.run(complete())
        call = result.choices[0].message.tool_calls[0]
        assert call.id == 'echo-1' and json.loads(call.function.arguments) == {'value': 'API_READY'}
        assert result.usage.prompt_tokens_details.cached_tokens == 8
    updates = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    if mode == 'early_disconnect':
        assert not updates
    else:
        assert len(updates) == 1 and updates[0]['event'] == 'pr5_model_stream_received'
        assert updates[0].keys() == {'event', 'phase', 'seconds'}


@pytest.mark.parametrize('during_stream', [False, True])
def test_worker_retries_current_request_and_repairs_receipt_without_repeating_write(monkeypatch, during_stream):
    from copy import deepcopy
    from openai import APIError

    monkeypatch.syspath_prepend(str(PATH.parents[1] / 'ple_entry'))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture-model')
    from pr5_adapter import ModelStreamInterrupted, ModelWorker

    calls = []
    writes = []

    async def write_file(path, content):
        writes.append((path, content))
        return len(content)

    def response(content=None, tools=None):
        message = SimpleNamespace(content=content, tool_calls=tools,
                                  model_dump=lambda **kwargs: {'role': 'assistant', 'content': content})
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])

    async def run():
        worker = ModelWorker(SimpleNamespace(time_left=lambda: 60, write_file=write_file))

        async def complete(phase, started, **kwargs):
            calls.append(deepcopy(kwargs))
            if len(calls) == 1:
                function = SimpleNamespace(name='write_file', arguments='{"path":"out","content":"saved"}')
                return response(tools=[SimpleNamespace(id='write-1', function=function)])
            if len(calls) == 2:
                if during_stream:
                    raise ModelStreamInterrupted('fixture stream interrupted after data')
                raise APIError('fixture request failed', request=httpx.Request('POST', 'https://fixture.invalid'), body=None)
            if len(calls) == 3:
                return response('{"files":')
            return response('{"files":{"out":"saved"}}')

        worker.complete = complete
        try:
            return await worker.run('Continue the assigned work.', owned=['out'], continuing=True)
        finally:
            await worker.client.close()

    if not during_stream:
        with pytest.raises(APIError, match='fixture request failed'):
            asyncio.run(run())
        assert len(calls) == 2
        assert writes == [('out', 'saved')]
        return
    assert asyncio.run(run()) == {'files': {'out': 'saved'}}
    assert writes == [('out', 'saved')]
    assert len(calls) == 4 and calls[1] == calls[2]
    assert calls[1]['messages'][-1]['role'] == 'tool'
    assert 'tools' not in calls[3]
    assert 'Preserve completed calculations' in calls[0]['messages'][0]['content']



@pytest.mark.parametrize('eventually_succeeds', [False, True])
def test_root_retries_interrupted_request_five_times(monkeypatch, eventually_succeeds):
    from copy import deepcopy

    monkeypatch.syspath_prepend(str(PATH.parents[1] / 'ple_entry'))
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    monkeypatch.setenv('MODEL_NAME', 'fixture-model')
    from pr5_adapter import ModelStreamInterrupted, ModelWorker

    reads = []
    requests = []

    def reply(content, tools=()):
        message = SimpleNamespace(
            content=content, tool_calls=tools,
            model_dump=lambda **kwargs: {'role': 'assistant', 'content': content},
        )
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])

    async def read_file(path):
        reads.append(path)
        return b'original artifact'

    async def complete(phase, started, **kwargs):
        assert phase == 'root'
        requests.append(deepcopy(kwargs))
        if len(requests) == 1:
            inspect = SimpleNamespace(name='read_file', arguments='{"path":"out"}')
            return reply(None, [SimpleNamespace(id='read-1', function=inspect)])
        if len(requests) <= 6 or not eventually_succeeds:
            raise ModelStreamInterrupted('fixture stream interrupted after data')
        return reply('{"status":"passed"}')

    async def run():
        worker = ModelWorker(SimpleNamespace(time_left=lambda: 60, read_file=read_file))
        worker.complete = complete
        try:
            return await worker.run('Verify the existing artifact.', review=True)
        finally:
            await worker.client.close()

    if eventually_succeeds:
        assert asyncio.run(run()) == {'status': 'passed'}
    else:
        with pytest.raises(ModelStreamInterrupted):
            asyncio.run(run())
    assert reads == ['out']
    assert len(requests) == 7
    assert all(request == requests[1] for request in requests[1:])
    assert requests[1]['messages'][-1]['role'] == 'tool'
