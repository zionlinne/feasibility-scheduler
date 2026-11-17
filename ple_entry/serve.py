"""Wrap the official PLE agent loop with local FBS stage scheduling."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import taskbridge
from fbs_adapter import run_session

PLANNER_SUFFIX = '''
Return ONLY JSON with these exact fields:
{"summary":"method and task decomposition", "deliverables":[
 {"path":"exact requested output path", "description":"requirements"}],
 "checks":[{"command":"an offline executable verification command", "timeout_sec":120}]}
List every requested deliverable. Do not add unnecessary outputs. Checks must
verify files, units, calculations, or requested content, not simply echo PASS.
Do not access grading scripts, references, old runs, or artifacts directories.
'''


async def solve(session):
    # Import only after the evaluator has supplied model configuration.
    import worker_loop as loop
    from mcp_tools import TaskMcpTools, TOOLS as MCP_TOOLS
    original_run_tool = loop.run_tool

    async def routed_tool(view, fn):
        if fn['name'] in ('task_mcp_tools','task_mcp_call'):
            return await view.mcp_router.run(fn)
        return await original_run_tool(view, fn)

    loop.run_tool = routed_tool
    async def invoke(stage, view, prompt):
        system = {'plan': loop.PLANNER,
                  'work': loop.WORKER,
                  'verify': loop.VERIFIER}[stage]
        tools = loop.TOOLS if stage == 'work' else loop.READ_ONLY
        tools = tools + MCP_TOOLS
        view.mcp_router = TaskMcpTools(view, readonly=stage != 'work')
        # Fresh messages for every stage; the verifier receives accepted plan,
        # actual worker summary and host-produced check results.
        messages = loop.conversation(system, view, prompt)
        if stage == 'plan':
            messages[0]['content'] += PLANNER_SUFFIX
        messages[0]['content'] += ('\nTask MCP server names: '
            + json.dumps([s['name'] for s in view.mcp_servers])
            + '. Discover their tools when needed. Review and planning roles may only use read-only MCP tools.')
        return await loop.agent(view, messages,
                                tools, min(20, (view.time_left() or 200) * .1))

    try:
        result = await run_session(session, invoke, os.environ['MODEL_NAME'])
        # State metadata only: never print prompts, tool args, paths or reports.
        print(json.dumps({'event':'fbs_finished', 'ok':result.ok,
                          'states':result.states, 'event_count':len(result.events)}), flush=True)
        return result.summary
    except Exception:
        # SDK errors can embed task text or secrets. Do not log exception bodies.
        print('{"event":"fbs_stopped","ok":false}', flush=True)
        return 'The run stopped before FBS verification completed. Existing deliverables may be incomplete.'
    finally:
        loop.run_tool = original_run_tool


if __name__ == '__main__':
    taskbridge.run(solve)
