"""PLE transport around PR5's unchanged native runner and acceptance hooks."""
from __future__ import annotations
import asyncio
import csv
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile

from feasibility_scheduler.adapters.legacy_doc import LegacyDocConfig, run_legacy_doc
from mcp_tools import TaskMcpTools, TOOLS as MCP_TOOLS


def event(kind, **values):
    print(json.dumps({'event':kind,**values}),flush=True)


def output_spec(instruction):
    archives=re.findall(r'(/[^\s`<>]+/output/[^\s`<>]+\.zip)',instruction)
    files=list(dict.fromkeys(re.findall(r'\|\s*`([^`/]+\.(?:csv|json|md|txt))`\s*\|',instruction)))
    if not archives or not files:
        raise ValueError('This adapter needs a declared ZIP output and a named text-file table')
    for name in files:
        if PurePosixPath(name).name!=name or '..' in name or '\\' in name:
            raise ValueError('Invalid output name')
    return archives[0],files


def parse_object(text):
    text=text.strip()
    if text.startswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    value=json.loads(text)
    if not isinstance(value,dict): raise ValueError('Expected JSON object')
    return value


def tool(name,props):
    return {'type':'function','function':{'name':name,'description':name+' inside the official task container',
      'parameters':{'type':'object','properties':props,'required':list(props)}}}


READ_TOOLS=[tool('bash',{'command':{'type':'string'},'timeout_sec':{'type':'integer'}}),
            tool('read_file',{'path':{'type':'string'}})]
WRITE_TOOL=tool('write_file',{'path':{'type':'string'},'content':{'type':'string'}})


class ModelWorker:
    def __init__(self,session):
        from openai import AsyncOpenAI
        self.session=session
        self.client=AsyncOpenAI(max_retries=3,timeout=max(1,session.time_left() or 21600))
        self.model=os.environ['MODEL_NAME']

    async def run(self,prompt,*,planning=False,review=False):
        rules=('You execute a request dispatched by the existing PR5 FBS harness. '
          'Follow its current contract, ownership, checks and output JSON schema. '
          'This is a physical-engineering task; software-specific examples are protocol examples. '
          'Use supplied engineering libraries inside the task container for calculations. '
          'Never inspect grading scripts, solutions, previous runs or external services. '
          'Keep source documents unchanged. Scratch scripts may be placed under /tmp. '
          'The original task documents are available through the official task tools. '
          'Return exactly the requested JSON object, without Markdown. '
          'Do not ask the operator to do work or create a separate planning workflow. ')
        if planning:
            rules+=('This is PR5 contract generation, not solving the engineering problem. '
                    'All small source documents are provided below. Use that material and return '
                    'the contract in one response; leave design calculations to dispatched work units. '
                    'Registered host checker ple-format checks delivery parsing only. Registered host '
                    'checker ple-root-review is a fresh model review that inspects actual files and '
                    'recomputes engineering constraints with task tools. It returns failed or unverified '
                    'when evidence is inadequate. It is not a professional certification or the official grader. ')
        elif review:
            rules+=('This is the host Root checker invoked by PR5. Inspect actual delivered files '
                    'and recompute relevant quantities with tools. Do not modify files. '
                    'A plausible narrative or nonempty file is not evidence of engineering compliance. ')
        else:
            rules+=('Compute using task tools, then deliver ALL current owned files as complete '
                    'text contents in the PR5 files map. Do not replace files with paths or summaries. ')
        tools=[] if planning else READ_TOOLS+([] if review else [WRITE_TOOL])+MCP_TOOLS
        router=TaskMcpTools(self.session,readonly=review)
        messages=[{'role':'system','content':rules},{'role':'user','content':prompt}]
        operations=0
        while True:
            left=self.session.time_left()
            if left is not None and left<=0: raise TimeoutError('Official task time exhausted')
            kwargs={'model':self.model,'messages':messages,'reasoning_effort':'high',
                    'max_completion_tokens':64000}
            if tools: kwargs.update(tools=tools,tool_choice='auto')
            response=await asyncio.wait_for(self.client.chat.completions.create(**kwargs),timeout=left)
            if response.usage:
                u=response.usage
                await self.session.usage(self.model,u.prompt_tokens,u.completion_tokens,
                    getattr(getattr(u,'prompt_tokens_details',None),'cached_tokens',0) or 0)
            message=response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                result=parse_object(message.content or '')
                if review and operations==0:
                    raise ValueError('Root did not inspect the actual task environment')
                return result
            for call in message.tool_calls:
                fn=call.function;args=json.loads(fn.arguments);operations+=1
                try:
                    if fn.name=='bash': result=await self.session.exec(args['command'],timeout_sec=args['timeout_sec'])
                    elif fn.name=='read_file':
                        result={'content':(await self.session.read_file(args['path'])).decode('utf8')}
                    elif fn.name=='write_file' and not review:
                        result={'bytes':await self.session.write_file(args['path'],args['content'])}
                    elif fn.name in ('task_mcp_tools','task_mcp_call'):
                        result=await router.run({'name':fn.name,'arguments':fn.arguments})
                    else: raise ValueError('Tool not available')
                except Exception as exc:
                    result={'error_type':type(exc).__name__}
                text=json.dumps(result,ensure_ascii=False)
                if len(text)>80000: text=text[:40000]+'\n[truncated; read smaller sections]\n'+text[-40000:]
                messages.append({'role':'tool','tool_call_id':call.id,'content':text})


async def source_materials(session,root):
    program=('import json,pathlib; p=pathlib.Path('+repr(root)+'); '
      'print(json.dumps([str(x) for x in p.rglob("*") if x.is_file() '
      'and x.suffix.lower() in (".csv",".md",".json",".txt") and x.stat().st_size<131072 '
      'and not set(x.relative_to(p).parts)&{"output","tests","solutions","verifier",".git"}]))')
    result=await session.exec('python3 -c '+shlex.quote(program))
    if result.get('exit_code')!=0: raise ValueError('Could not list supplied material')
    references={}
    for name in json.loads(result['output']):
        relative=PurePosixPath(name).relative_to(root).as_posix()
        if relative=='task.md': continue
        references['provided/'+relative]=(await session.read_file(name)).decode('utf8')
    return references


def format_check(workspace,names):
    for name in names:
        p=workspace/name
        if not p.is_file() or p.is_symlink() or not p.stat().st_size:
            return {'status':'failed','detail':'Missing/nonregular output: '+name}
        text=p.read_text(encoding='utf8')
        if p.suffix=='.json': json.loads(text)
        if p.suffix=='.csv':
            rows=list(csv.reader(io.StringIO(text)))
            if len(rows)<2 or not rows[0] or any(len(r)!=len(rows[0]) for r in rows[1:]):
                return {'status':'failed','detail':'Malformed/empty CSV: '+name}
    return {'status':'passed','detail':'Text/CSV/JSON delivery syntax only; engineering checked separately'}


async def publish(session,workspace,names,archive):
    output=str(PurePosixPath(archive).parent)
    for name in names:
        await session.write_file(output+'/'+name,(workspace/name).read_bytes())
    program=('import pathlib,zipfile; p=pathlib.Path('+repr(output)+'); '
      'z=zipfile.ZipFile('+repr(archive)+',"w",zipfile.ZIP_DEFLATED); '
      '[z.write(p/n,n) for n in '+repr(names)+']; z.close()')
    result=await session.exec('python3 -c '+shlex.quote(program))
    if result.get('exit_code')!=0: raise ValueError('ZIP packaging failed')


async def solve(session):
    archive,names=output_spec(session.instruction)
    root=str(PurePosixPath(archive).parent.parent)
    task_source=(await session.read_file(root+'/task.md')).decode('utf8')
    references=await source_materials(session,root)
    worker=ModelWorker(session);loop=asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix='pr5-ple-') as temporary:
        directory=Path(temporary);bridge=directory/'native';bridge.mkdir()
        source=directory/'source';source.mkdir()
        (source/'TASK.md').write_text(task_source,encoding='utf8')
        profile={'id':'ple-engineering-v1','categories':[
          {'id':'delivery','description':'Required files are complete and parseable','applicable':True,
           'allowed_kinds':['file_exists','text_contains','json_equals','json_match','external'],
           'checker_names':['ple-format','ple-root-review']},
          {'id':'engineering','description':'Original engineering obligations and cross-file consistency are substantively checked',
           'applicable':True,'allowed_kinds':['external'],'checker_names':['ple-root-review']}]}
        task_input={'modules':[{'module_id':'submission','title':'Complete the requested engineering package',
          'owned_files':names,'outputs':names,'deps':[],'estimated_tokens':24000}],
          'reference_files':references,'external_checker_names':['ple-format','ple-root-review'],
          'acceptance_profile':profile}
        (source/'TASK_INPUT.json').write_text(json.dumps(task_input),encoding='utf8')

        async def review(workspace):
            checked=format_check(workspace,names)
            if checked['status']!='passed': return checked
            before={n:hashlib.sha256((workspace/n).read_bytes()).hexdigest() for n in names}
            await publish(session,workspace,names,archive)
            event('pr5_root_review_started')
            prompt=(task_source+'\n\nActual output directory: '+str(PurePosixPath(archive).parent)
              +'\nInspect all delivered files and verify the task requirements, calculations, constraints, '
               'resource/day consistency and citations from supplied source documents. '
               'Return {"status":"passed|failed|unverified","detail":"specific evidence and defects",'
               '"criteria":[{"requirement":"...","status":"passed|failed|unverified","evidence":"..."}]}. '
               'Use unverified when evidence is insufficient. Do not claim professional certification.')
            answer=await worker.run(prompt,review=True)
            status=answer.get('status');criteria=answer.get('criteria')
            if status not in ('passed','failed','unverified') or not isinstance(criteria,list) or not criteria:
                raise ValueError('Invalid Root review')
            if status=='passed' and any(c.get('status')!='passed' or not c.get('evidence') for c in criteria):
                raise ValueError('Root pass does not match its evidence')
            if before!={n:hashlib.sha256((workspace/n).read_bytes()).hexdigest() for n in names}:
                return {'status':'unverified','detail':'Candidate changed during Root review'}
            directory=str(PurePosixPath(archive).parent)
            check=('import json,hashlib,pathlib; p=pathlib.Path('+repr(directory)+'); '
                   'print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in '+repr(names)+'}))')
            inspected=await session.exec('python3 -c '+shlex.quote(check))
            if inspected.get('exit_code')!=0 or json.loads(inspected['output'])!=before:
                return {'status':'unverified','detail':'Delivered task files changed during Root review'}
            event('pr5_root_review_finished',status=status)
            return {'status':status,'detail':json.dumps(answer,ensure_ascii=False)}

        def root_checker(workspace):
            return asyncio.run_coroutine_threadsafe(review(workspace),loop).result()

        remaining=max(1,session.time_left() or 21600)
        cfg=LegacyDocConfig(task_input_path=source/'TASK_INPUT.json',task_md_path=source/'TASK.md',
          workdir=directory/'run',client_kind='native',native_bridge_dir=bridge,
          policy='rewrite',acceptance='loose',timeout_seconds=remaining,lease_seconds=remaining+60,
          acceptance_profile=profile,external_checkers={
            'ple-format':lambda workspace:format_check(workspace,names),'ple-root-review':root_checker})
        done=asyncio.create_task(asyncio.to_thread(run_legacy_doc,cfg))
        handled=set();active=set();seen_events=set()

        async def execute(request_path):
            request=json.loads(request_path.read_text(encoding='utf8'))
            target=request_path.with_name(request_path.name.replace('.request.json','.response.json'))
            task_id=request['task_id'];planning=task_id=='contract_planning'
            event('pr5_native_dispatch',task_id=task_id,planning=planning)
            try:
                body=await worker.run(request['prompt'],planning=planning)
                envelope={'status':'success','producer':'subscription-model','body':body}
            except Exception as exc:
                event('pr5_native_error',task_id=task_id,error_type=type(exc).__name__)
                envelope={'status':'error','producer':'subscription-model','body':{'error_type':type(exc).__name__}}
            tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(envelope),encoding='utf8');tmp.replace(target)
            event('pr5_native_response',task_id=task_id)

        event('pr5_runner_started',policy=cfg.policy,max_concurrency=cfg.max_concurrency,
              official_seconds_remaining=round(remaining),source_files=len(references))
        try:
            while not done.done():
                audit=directory/'run'/'events.jsonl'
                if audit.exists():
                    for line in audit.read_text(encoding='utf8').splitlines():
                        try: row=json.loads(line)
                        except ValueError: continue
                        if row['event_id'] not in seen_events:
                            seen_events.add(row['event_id'])
                            event('pr5_scheduler_event',type=row['type'],task_id=row.get('task_id'),
                                  state=row.get('new_state'),sequence=row.get('sequence'))
                for request in bridge.glob('*.request.json'):
                    if request.name not in handled:
                        handled.add(request.name)
                        future=asyncio.create_task(execute(request));active.add(future)
                        future.add_done_callback(active.discard)
                await asyncio.sleep(.1)
            outcome=await done
            workspace=directory/'run'/'workspace'
            if all((workspace/n).is_file() for n in names):
                await publish(session,workspace,names,archive)
            event('pr5_finished',ok=outcome.ok,states={k:v.value for k,v in outcome.states.items()})
            return 'PR5 execution finished; host acceptance='+str(outcome.ok)+'. Deliverable: '+archive
        except Exception as exc:
            event('pr5_run_error',error_type=type(exc).__name__,detail=str(exc)[:500])
            raise
        finally:
            for future in active: future.cancel()
