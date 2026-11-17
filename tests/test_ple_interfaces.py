"""Transport/MCP boundaries only; no model or engineering-task simulation."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ENTRY=Path(__file__).resolve().parents[1]/'ple_entry'
sys.path.insert(0,str(ENTRY))
from mcp_tools import TaskMcpTools

spec=importlib.util.spec_from_file_location('ple_interface_entry',ENTRY/'serve.py')
entry=importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


def test_entry_passes_original_session_and_uses_no_additional_clock(monkeypatch):
    session=SimpleNamespace(time_left=lambda:21600, instruction='interface test')
    module=ModuleType('test_pr5_handler')
    observed=[]
    async def handler(received):
        observed.append(received)
        assert received.time_left()==21600
        return 'handler result'
    module.run=handler
    monkeypatch.setitem(sys.modules,module.__name__,module)
    monkeypatch.setenv('FBS_PLE_ENTRYPOINT','test_pr5_handler:run')
    assert asyncio.run(entry.solve(session))=='handler result'
    assert observed==[session]


def test_missing_adapter_fails_explicitly_instead_of_using_old_workflow(monkeypatch):
    monkeypatch.delenv('FBS_PLE_ENTRYPOINT',raising=False)
    with pytest.raises(RuntimeError,match='not configured'):
        asyncio.run(entry.solve(SimpleNamespace()))


class Client:
    async def initialize(self): return {}
    async def list_tools(self):
        return [{'name':'read','annotations':{'readOnlyHint':True}},
                {'name':'edit','annotations':{'readOnlyHint':False}}]
    async def call_tool(self,name,arguments):
        return {'content':[{'type':'text','text':name}]}


@pytest.mark.parametrize('readonly,name,allowed',[(True,'read',True),(True,'edit',False),(False,'edit',True)])
def test_task_mcp_access_boundaries(readonly,name,allowed):
    env=SimpleNamespace(mcp_servers=[{'name':'docs','url':'http://127.0.0.1:8000/mcp'}])
    router=TaskMcpTools(env,readonly=readonly,client_factory=lambda *a:Client())
    call={'name':'task_mcp_call','arguments':json.dumps({'server':'docs','name':name,'arguments':{}})}
    result=asyncio.run(router.run(call))
    assert ('not permitted' not in result)==allowed


def test_task_mcp_rejects_undeclared_server_without_connecting():
    def forbidden(*args): raise AssertionError('Must not connect')
    router=TaskMcpTools(SimpleNamespace(mcp_servers=[]),client_factory=forbidden)
    call={'name':'task_mcp_tools','arguments':json.dumps({'server':'outside','query':''})}
    assert 'not permitted' in asyncio.run(router.run(call))
