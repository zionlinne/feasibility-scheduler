"""PLE transport around PR5's unchanged native runner and acceptance hooks."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import tempfile
from time import monotonic

from feasibility_scheduler.adapters.legacy_doc import LegacyDocConfig, run_legacy_doc, build_legacy_tasks
from mcp_tools import TaskMcpTools, TOOLS as MCP_TOOLS
from ple_delivery import (Delivery, EvidenceSession, declared_package, first_reads,
                          normalize_spec, task_root)
from ple_contract import prepare_contract


def event(kind, **values):
    print(json.dumps({'event':kind,**values}),flush=True)


def host_profile():
    return {'id':'ple-engineering-v1','categories':[
        {'id':'delivery','description':'Required files are complete and parseable','applicable':True,
         'allowed_kinds':['file_exists','text_contains','json_equals','json_match','external'],
         'checker_names':['ple-format','ple-package-evidence','ple-root-review']},
        {'id':'package_execution','description':'Exact delivery paths, current archive members and actual host execution/read-order evidence',
         'applicable':True,'allowed_kinds':['external'],'checker_names':['ple-package-evidence']},
        {'id':'engineering','description':'Original engineering obligations and cross-file consistency are substantively checked',
         'applicable':True,'allowed_kinds':['external'],'checker_names':['ple-root-review']}]}


def parse_object(text):
    text=text.strip()
    if text.startswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
    value=json.loads(text)
    if not isinstance(value,dict): raise ValueError('Expected JSON object')
    return value


async def import_task_spec(worker, prompt, root, event):
    """Import task I/O with one bounded, schema-directed correction attempt."""
    first = None
    try:
        first = await worker.run(prompt, importing=True)
        return normalize_spec(first, root)
    except (KeyError, TypeError, ValueError) as first_error:
        detail = f'{type(first_error).__name__}: {first_error}'[:500]
        try:
            previous = json.dumps(first, ensure_ascii=False)[:6000]
        except (TypeError, ValueError):
            previous = repr(first)[:6000]
        event('pr5_task_io_import_invalid', attempt=1, detail=detail)
        correction = (
            prompt
            + '\n\nThe previous importer response failed host validation: '
            + detail
            + '\nPrevious response (possibly truncated): '
            + previous
            + '\nRe-read only the task brief and referenced output instructions. '
              'Return a corrected JSON object matching the required schema. '
              'Declare every explicitly required output file. Use kind=binary for '
              'XLSX, PDF, DOCX and other binary formats; use kind=text for UTF-8 text. '
              'Use kind=final_message only when a substantive final response is required '
              'and no file deliverable is required. Do not invent filenames, paths, or '
              'archive members. An archive may list only files explicitly declared as '
              'deliverables. If the brief is genuinely ambiguous, do not guess.'
        )
        try:
            second = await worker.run(correction, importing=True)
            return normalize_spec(second, root)
        except (KeyError, TypeError, ValueError) as second_error:
            event('pr5_task_io_import_failed', attempt=2,
                  detail=f'{type(second_error).__name__}: {second_error}'[:500])
            raise ValueError(
                'Task I/O import failed after one correction attempt; '
                f'first validation error: {detail}; '
                f'second validation error: {type(second_error).__name__}: {second_error}'
            ) from second_error


def canonicalize_paths(body,aliases,*,planning):
    """Translate only declared artifact aliases, never predicate values or text."""
    body=json.loads(json.dumps(body))
    def paths(obj,key):
        if isinstance(obj.get(key),list): obj[key]=[aliases.get(x,x) if isinstance(x,str) else x for x in obj[key]]
    if planning:
        checks=body.get('checks')
        for check in checks if isinstance(checks,list) else []:
            if not isinstance(check,dict): continue
            for key in ('path','other_path'):
                if isinstance(check.get(key),str):check[key]=aliases.get(check[key],check[key])
            for key in ('paths','read_paths','repair_paths'):paths(check,key)
        modules=body.get('modules')
        for module in modules if isinstance(modules,list) else []:
            if isinstance(module,dict): paths(module,'owned_files')
        proposals=body.get('decompositions')
        for proposal in proposals if isinstance(proposals,list) else []:
            if not isinstance(proposal,dict): continue
            children=proposal.get('children')
            for child in children if isinstance(children,list) else []:
                if isinstance(child,dict): paths(child,'owned_files')
    else:
        if isinstance(body.get('files'),dict):
            projected={}
            for path,content in body['files'].items():
                canonical=aliases.get(path,path)
                if canonical in projected:raise ValueError('Duplicate artifact aliases')
                projected[canonical]=content
            body['files']=projected
        paths(body,'manifest')
        if isinstance(body.get('file'),dict):
            path=body['file'].get('path')
            if isinstance(path,str):body['file']['path']=aliases.get(path,path)
    return body


def tool(name,props):
    return {'type':'function','function':{'name':name,'description':name+' inside the official task container',
      'parameters':{'type':'object','properties':props,'required':list(props)}}}


READ_TOOLS=[tool('bash',{'command':{'type':'string'},'timeout_sec':{'type':'integer'}}),
            tool('read_file',{'path':{'type':'string'}})]
WRITE_TOOL=tool('write_file',{'path':{'type':'string'},'content':{'type':'string'}})


class ModelWorker:
    def __init__(self,session,delivery=None):
        from openai import AsyncOpenAI
        self.session=session
        self.client=AsyncOpenAI(max_retries=3,timeout=max(1,session.time_left() or 21600))
        self.model=os.environ['MODEL_NAME']
        self.stream_model=os.environ.get('FBS_PLE_MODEL_STREAM') == '1'
        self.delivery=delivery

    async def complete(self, phase, started, **kwargs):
        completions = self.client.chat.completions
        if not self.stream_model:
            return await completions.create(**kwargs)

        next_update = started
        async with completions.stream(**kwargs, stream_options={'include_usage': True}) as stream:
            async for update in stream:
                if update.type == 'chunk' and monotonic() >= next_update:
                    now = monotonic()
                    event('pr5_model_stream_received', phase=phase, seconds=round(now - started, 1))
                    next_update = now + 30
            response = await stream.get_final_completion()
        if not response.choices or not response.choices[0].finish_reason:
            raise ValueError('Model stream ended before the completion finished')
        return response

    async def run(self,prompt,*,planning=False,review=False,importing=False,protocol_feedback=False):
        rules=(('You import task I/O for the PR5 adapter. ' if importing else
                'You execute a request dispatched by the existing PR5 FBS harness. ')+
          'Follow its current contract, ownership, checks and output JSON schema. '
          'This is a physical-engineering task; software-specific examples are protocol examples. '
          'Use supplied engineering libraries inside the task container for calculations. '
          'Never inspect grading scripts, solutions, previous runs or external services. '
          'Keep source documents unchanged. Scratch scripts may be placed under /tmp. '
          'The original task documents are available through the official task tools. '
          'Return exactly the requested JSON object, without Markdown. '
          'Do not ask the operator to do work or create a separate planning workflow. ')
        if protocol_feedback:
            rules+=('Return only the requested JSON additions to missing contract fields. '
                    'Do not regenerate the full contract, solve the engineering task, use tools, '
                    'weaken existing obligations, or erase real blockers. Values must be grounded '
                    'in the supplied task and the existing contract. ')
        elif importing:
            rules+=('Import task I/O only; do not solve or design a workflow. Read the brief and '
                    'only the referenced material needed to identify actual requested outputs. '
                    'Return {"files":[{"path":"absolute task-container path","kind":"text|binary|final_message"}],'
                    '"archives":[{"path":"absolute ZIP path","members":{"archive/member":"absolute source file path"}}]}. '
                    'Use kind=text for UTF-8 outputs, binary for office/CAD/PDF/image/archive files written '
                    'by tools, and final_message when a substantive final response or MCP-side-effect summary '
                    'is required. final_message needs no path. archives is ONLY for a package whose full '
                    'member set the brief explicitly declares; otherwise declare the archive as a binary file. '
                    'Do not include input/reference files as outputs. Never invent required files. ')
        elif planning:
            rules+=('This is PR5 contract generation, not solving the engineering problem. '
                    'Collected small text source documents are provided below. Use that material and return '
                    'the contract in one response; leave design calculations to dispatched work units. '
                    'Registered host checker ple-format checks delivery parsing only. Registered host '
                    'checker ple-root-review is a fresh model review that inspects actual files and '
                    'recomputes engineering constraints with task tools. It returns failed or unverified '
                    'when evidence is inadequate. It is not a professional certification or the official grader. ')
            rules+=('PR5 contract paths MUST use the exact canonical relative names in MODULES. '
                    'Absolute paths in TASK describe the remote environment, not contract paths. '
                    'Provided-material check paths use their exact registered provided/ names. ')
            rules+=('The host also registers ple-package-evidence: at acceptance it publishes current '
                    'FBS text outputs, packages declared archive members, independently checks member '
                    'names and SHA-256 against the current files at the exact requested destination, '
                    'checks host preloading read order and reports the first complete candidate host '
                    'operation. The runtime attempts early packaging after worker tool operations; '
                    'the approximate step milestone is an advisory target, not proof of a particular '
                    'official step count. These future acceptance procedures are available before work '
                    'starts; a final output need not already exist. '
                    'Missing hidden evaluation indexes, held-out selections or certified optimum bounds '
                    'that TASK explicitly says are unavailable by design are not missing execution inputs. '
                    'Keep all public thresholds and engineering obligations; pursue them using the public '
                    'corpus. Record inability to certify exact hidden recall, score or optimality in '
                    'acceptance_limitations and handoff. Grading-method descriptions do not create a '
                    'requirement for the host to certify an unavailable hidden score before starting. '
                    'Real missing mandatory inputs, unavailable tools or uncovered public mandatory '
                    'acceptance obligations remain blocking questions in unresolved. Never erase them. ')
        elif review:
            rules+=('This is the host Root checker invoked by PR5. Inspect actual delivered files '
                    'and recompute relevant quantities with tools. Do not modify files. '
                    'A plausible narrative or nonempty file is not evidence of engineering compliance. ')
        else:
            rules+=('Compute using task tools, then deliver ALL current owned files as complete '
                    'text contents in the PR5 files map. Its keys MUST be the exact relative owned_files '
                    'names, not absolute remote paths. Do not replace files with paths or summaries. ')
        if self.delivery is not None:
            rules+='\nHOST TASK DELIVERY MAP (read-only): '+json.dumps(self.delivery.spec)+'\n'
            rules+=('For kind=binary, generate the real file inside the task container using tools. '
                    'Its owned receipt artifact is a JSON text file: return its full JSON text in files '
                    'with at least {"path":"the declared remote path"}; the host captures actual size/hash '
                    'from disk before passing the receipt to PR5. A receipt is transport evidence only, '
                    'not proof of format or engineering validity. Include any generation script text and '
                    'reproduction command in the receipt when feasible. Never put binary bytes into a '
                    'UTF-8 files value. For kind=text, deliver the actual complete text, and write an early '
                    'version to its declared remote output path as soon as it is complete. '
                    'Modify only remote outputs mapped from your current owned_files or repair_files; '
                    'the full delivery map grants no permission to overwrite another module output. '
                    'The host owns only the packaging of archives explicitly listed in archives. '
                    'Engineering review must independently verify every required deliverable, all '
                    'unknown format checks, and the completeness of this I/O map against the original '
                    'task; omissions or invented requirements fail review. ')
        tools=[] if planning else READ_TOOLS+([] if review or importing else [WRITE_TOOL])+MCP_TOOLS
        router=TaskMcpTools(self.session,readonly=review or importing)
        messages=[{'role':'system','content':rules},{'role':'user','content':prompt}]
        operations=0
        calls=0
        phase_started=monotonic()
        phase = 'root' if review else 'import' if importing else 'plan' if planning else 'worker'
        while True:
            left=self.session.time_left()
            if left is not None and left<=0: raise TimeoutError('Official task time exhausted')
            kwargs={'model':self.model,'messages':messages,'reasoning_effort':'high',
                    'max_completion_tokens':64000}
            if tools: kwargs.update(tools=tools,tool_choice='auto')
            started = monotonic()
            calls+=1
            event('pr5_model_call_started', phase=phase, calls=calls, tools=operations)
            try:
                response=await asyncio.wait_for(self.complete(phase,started,**kwargs),timeout=left)
            except Exception:
                event('pr5_model_call_failed', phase=phase, seconds=round(monotonic()-started, 1))
                raise
            u=response.usage
            cached=getattr(getattr(u,'prompt_tokens_details',None),'cached_tokens',0) or 0
            event('pr5_model_call_finished', phase=phase, seconds=round(monotonic()-started, 1),
                  input_tokens=u.prompt_tokens if u else 0,
                  output_tokens=u.completion_tokens if u else 0, cached_tokens=cached)
            if response.usage:
                await self.session.usage(self.model,u.prompt_tokens,u.completion_tokens,cached)
            message=response.choices[0].message
            messages.append(message.model_dump(exclude_none=True))
            if not message.tool_calls:
                result=parse_object(message.content or '')
                if review and operations==0:
                    raise ValueError('Root did not inspect the actual task environment')
                event('pr5_model_phase_finished', phase=phase, seconds=round(monotonic()-phase_started, 1),
                      calls=calls, tools=operations)
                return result
            for call in message.tool_calls:
                fn=call.function;args=json.loads(fn.arguments);operations+=1
                try:
                    if fn.name=='bash': result=await self.session.exec(args['command'],timeout_sec=args['timeout_sec'])
                    elif fn.name=='read_file':
                        data=await self.session.read_file(args['path'])
                        try: result={'content':data.decode('utf8')}
                        except UnicodeError:
                            result={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),
                                    'detail':'Binary input: use task bash or MCP to parse/inspect it; this text is not its content'}
                    elif fn.name=='write_file' and not review:
                        result={'bytes':await self.session.write_file(args['path'],args['content'])}
                    elif fn.name in ('task_mcp_tools','task_mcp_call'):
                        result=await router.run({'name':fn.name,'arguments':fn.arguments})
                    else: raise ValueError('Tool not available')
                except Exception as exc:
                    result={'error_type':type(exc).__name__,'detail':str(exc)[:1000]}
                if self.delivery is not None and not review and not importing and fn.name in ('bash','write_file','task_mcp_call'):
                    try:
                        evidence=await self.delivery.checkpoint()
                        result={'tool_result':result,'complete_delivery_on_disk':evidence['complete']}
                    except Exception as exc:
                        result={'tool_result':result,'delivery_checkpoint_error':type(exc).__name__}
                text=json.dumps(result,ensure_ascii=False)
                if len(text)>80000: text=text[:40000]+'\n[truncated; read smaller sections]\n'+text[-40000:]
                messages.append({'role':'tool','tool_call_id':call.id,'content':text})


async def source_materials(session,root,initial_reads=()):
    references={}
    for name in initial_reads:
        relative=PurePosixPath(name).relative_to(root).as_posix()
        references['provided/'+relative]=(await session.read_file(name)).decode('utf8')
    if root=='/':
        return references  # No project root was declared: never crawl the OS filesystem.
    program=('import json,pathlib; p=pathlib.Path('+repr(root)+'); '
      'print(json.dumps([str(x) for x in p.rglob("*") if x.is_file() '
      'and x.suffix.lower() in (".csv",".md",".json",".txt") and x.stat().st_size<131072 '
      'and not set(x.relative_to(p).parts)&{"output","tests","solutions","verifier",".git"}]))')
    result=await session.exec('python3 -c '+shlex.quote(program))
    if result.get('exit_code')!=0: raise ValueError('Could not list supplied material')
    for name in json.loads(result['output']):
        relative=PurePosixPath(name).relative_to(root).as_posix()
        if relative=='task.md': continue
        if 'provided/'+relative in references: continue
        references['provided/'+relative]=(await session.read_file(name)).decode('utf8')
    return references


async def solve(session):
    session=EvidenceSession(session)
    root=task_root(session.instruction,session.workdir)
    required_reads=[]
    task_source=session.instruction
    if re.search(r'\bread\s+`task\.md`',session.instruction,re.I):
        task_path=str(PurePosixPath(root)/'task.md')
        task_source=(await session.read_file(task_path)).decode('utf8')
        required_reads.append(task_path)
    initial_reads=first_reads(task_source,root)
    required_reads+=initial_reads
    references=await source_materials(session,root,initial_reads)
    worker=ModelWorker(session)
    spec=declared_package(session.instruction+'\n'+task_source,root)
    if spec is None:
        event('pr5_task_io_import_started')
        import_prompt='Task root: '+root+'\n'+session.instruction+'\n'+task_source
        spec=await import_task_spec(worker,import_prompt,root,event)
        event('pr5_task_io_import_finished',files=len(spec['files']),archives=len(spec['archives']))
    delivery=Delivery(session,spec,required_reads,event)
    names=delivery.names
    aliases=delivery.aliases
    aliases.update({str(PurePosixPath(root)/name.removeprefix('provided/')):name for name in references})
    worker.delivery=delivery
    loop=asyncio.get_running_loop()
    with tempfile.TemporaryDirectory(prefix='pr5-ple-') as temporary:
        directory=Path(temporary);bridge=directory/'native';bridge.mkdir()
        source=directory/'source';source.mkdir()
        (source/'TASK.md').write_text(task_source,encoding='utf8')
        profile=host_profile()
        task_input={'modules':[{'module_id':'submission','title':'Complete the requested engineering package',
          'owned_files':names,'outputs':names,'deps':[],'estimated_tokens':24000}],
          'reference_files':references,'external_checker_names':['ple-format','ple-package-evidence','ple-root-review'],
          'acceptance_profile':profile}
        (source/'TASK_INPUT.json').write_text(json.dumps(task_input),encoding='utf8')

        review_round=0

        async def review(workspace):
            nonlocal review_round
            checked,evidence=await delivery.publish(workspace)
            if checked['status']!='passed': return checked
            before={n:hashlib.sha256((workspace/n).read_bytes()).hexdigest() for n in names}
            review_round+=1
            round_number=review_round
            event('pr5_root_review_started',round=round_number)
            prompt=(task_source+'\n\nHOST DELIVERY MAP:\n'+json.dumps(spec)
              +'\nACTUAL EXECUTION/PACKAGING EVIDENCE:\n'+json.dumps(evidence)
              +'\nACTUAL FBS TEXT ARTIFACTS:\n'+json.dumps({n:(workspace/n).read_text(encoding='utf8') for n in names})
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
            inspected=await delivery.inspect()
            fingerprint=lambda e:{r['path']:r.get('sha256') for r in e['files']+e['archives']}
            if fingerprint(inspected)!=fingerprint(evidence):
                return {'status':'unverified','detail':'Delivered task files changed during Root review'}
            event('pr5_root_review_finished',round=round_number,status=status,
                  detail=answer.get('detail',''),
                  rejected_criteria=[c for c in criteria if c.get('status')!='passed'])
            return {'status':status,'detail':json.dumps(answer,ensure_ascii=False)}

        def root_checker(workspace):
            return asyncio.run_coroutine_threadsafe(review(workspace),loop).result()

        def package_checker(workspace):
            checked,_=asyncio.run_coroutine_threadsafe(delivery.publish(workspace),loop).result()
            return checked

        remaining=max(1,session.time_left() or 21600)
        repair_limit=int(os.environ.get('FBS_PLE_CONTRACT_REPAIR_LIMIT','1'))
        cfg=LegacyDocConfig(task_input_path=source/'TASK_INPUT.json',task_md_path=source/'TASK.md',
          workdir=directory/'run',client_kind='native',native_bridge_dir=bridge,
          policy='rewrite',acceptance='loose',timeout_seconds=remaining,lease_seconds=remaining+60,
           contract_repair_limit=repair_limit,contract_stop_on_stall=True,
          acceptance_profile=profile,external_checkers={
            'ple-format':delivery.local_check,'ple-package-evidence':package_checker,
            'ple-root-review':root_checker})
        validation_tasks=[replace(t,payload={**t.payload,'acceptance_profile':profile,
                          'planning_standard':cfg.planning_standard})
                          for t in build_legacy_tasks(task_input,task_md=task_source)]
        done=asyncio.create_task(asyncio.to_thread(run_legacy_doc,cfg))
        handled=set();active=set();seen_events=set()

        async def execute(request_path):
            request=json.loads(request_path.read_text(encoding='utf8'))
            target=request_path.with_name(request_path.name.replace('.request.json','.response.json'))
            task_id=request['task_id'];planning=task_id=='contract_planning'
            repair = re.search(r':contract-repair:(\d+)$', request.get('idempotency_key',''))
            event('pr5_native_dispatch',task_id=task_id,planning=planning,
                  repair_round=int(repair.group(1)) if repair else None)
            try:
                body=await worker.run(request['prompt'],planning=planning)
                original=json.dumps(body,sort_keys=True)
                body=canonicalize_paths(body,aliases,planning=planning)
                if planning:
                    body=await prepare_contract(body,worker=worker,tasks=validation_tasks,
                        source=task_source,checkers=cfg.external_checkers,aliases=aliases,
                        canonicalize=canonicalize_paths,event=event)
                else:
                    body=await delivery.capture_binary(body,request.get('metadata',{}).get('owned_files',[]))
                if json.dumps(body,sort_keys=True)!=original:
                    event('pr5_transport_paths_canonicalized',task_id=task_id)
                if planning and body.get('unresolved')!=[]:
                    event('pr5_contract_blocked',
                          field_present='unresolved' in body,
                          field_type=type(body.get('unresolved')).__name__,
                          blockers=body.get('unresolved'),
                          response_fields=sorted(body))
                envelope={'status':'success','producer':'subscription-model','body':body}
            except Exception as exc:
                event('pr5_native_error',task_id=task_id,error_type=type(exc).__name__)
                envelope={'status':'error','producer':'subscription-model','body':{'error_type':type(exc).__name__}}
            tmp=target.with_suffix('.tmp');tmp.write_text(json.dumps(envelope),encoding='utf8');tmp.replace(target)
            event('pr5_native_response',task_id=task_id)

        event('pr5_runner_started',policy=cfg.policy,max_concurrency=cfg.max_concurrency,
              contract_repair_limit=cfg.contract_repair_limit,
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
                final_check,_=await delivery.publish(workspace)
                if final_check['status']!='passed':
                    raise ValueError('Final delivery integrity failed: '+final_check['detail'])
            event('pr5_finished',ok=outcome.ok,states={k:v.value for k,v in outcome.states.items()})
            return delivery.final_message(workspace,outcome.ok)
        except Exception as exc:
            event('pr5_run_error',error_type=type(exc).__name__,detail=str(exc)[:500])
            raise
        finally:
            for future in active: future.cancel()
