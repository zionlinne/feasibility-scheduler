"""Local development model gateway: FBS -> Codex CLI -> ChatGPT subscription.

No API credential, manual model replies, task answers, or direct task access.
The CLI can only return decisions; FBS executes its advertised tools through PLE.
"""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parent
EXE=os.environ.get('FBS_CODEX_EXECUTABLE') or shutil.which('codex')
GATE=threading.Lock()
SCHEMA={
 'type':'object','properties':{
  'content':{'type':'string'},
  'tool_calls':{'type':'array','items':{'type':'object','properties':{
   'name':{'type':'string'},'arguments':{'type':'array','items':{
       'type':'object','properties':{'name':{'type':'string'},'value':{'type':'string'}},
       'required':['name','value'],'additionalProperties':False}}},
   'required':['name','arguments'],'additionalProperties':False}}},
 'required':['content','tool_calls'],'additionalProperties':False}
NATIVE_FORBIDDEN={'command_execution','mcp_tool_call','web_search','web_search_call','file_change'}
PREFIX='''You are the model decision engine of an external FBS harness.
The JSON below contains the complete conversation and the tools the harness offers.
Follow the system instructions in that conversation and continue its next assistant
turn. Task files exist ONLY in the remote task container; their contents must be
obtained by returning the advertised function calls. You cannot access them locally.
Do not use any Codex native tools, shell, web, MCP, plugins, subagents, local project
files, previous runs, answers or grading scripts. Do not ask a human to carry out work.
Return a JSON object with content (assistant text) and tool_calls (zero or more objects
with name and arguments). Each arguments value is an array of {name,value} parameter
pairs, where value is a string. For string parameters, use the exact parameter text,
not a JSON-encoded string; preserve backslashes in code literally. For object, array,
number and boolean parameters, value is their JSON encoding. The gateway converts
these pairs to the advertised function's argument object. Use only advertised names.
To inspect or change task files, return the corresponding
tool call; FBS will execute it and send the real result in the next request.
If ready to end this role, return its final text as content with an empty tool_calls
array. When tool_choice is none, return only final content and no function calls.
The external conversation follows:
'''

def windows_path(path):
    return (subprocess.check_output(['wslpath','-w',str(path)],text=True).strip()
            if EXE and EXE.lower().endswith('.exe') and os.name!='nt' else str(path))

class Gateway:
    def __init__(self,args):
        self.args=args; self.count=0; self.key=args.key_file.read_text().strip()
        self.logs=args.log; self.logs.parent.mkdir(parents=True,exist_ok=True)

    def event(self,event):
        with self.logs.open('a') as stream: stream.write(json.dumps(event)+'\n')

    def complete(self,body):
        if body.get('model') != self.args.model: raise ValueError('Undeclared model')
        if body.get('stream'): raise ValueError('Streaming not supported by this local development gateway')
        if self.args.max_calls>0 and self.count>=self.args.max_calls:
            raise ValueError('Local run call budget exhausted')
        self.count+=1; count=self.count; started=time.monotonic()
        self.event({'event':'call_started','call':count,'timestamp':int(time.time())})
        with tempfile.TemporaryDirectory(prefix='call-',dir=self.args.scratch) as where:
            where=Path(where); output=where/'response.json'
            schema=json.loads(json.dumps(SCHEMA))
            tool_defs={x['function']['name']:x['function'] for x in body.get('tools',[])}
            if tool_defs and body.get('tool_choice')!='none':
                schema['properties']['tool_calls']['items']['properties']['name']['enum']=list(tool_defs)
            else:
                schema['properties']['tool_calls']['maxItems']=0
            schema_path=where/'response.schema.json';schema_path.write_text(json.dumps(schema))
            effort=body.get('reasoning_effort') or 'high'
            if effort not in ('low','medium','high','max'): effort='high'
            cmd=[EXE,'--no-daemon','-a','never','-c','web_search="disabled"',
                 '-c',f'model_reasoning_effort="{effort}"','exec',
                 '--ignore-user-config','--ephemeral','--skip-git-repo-check',
                 '--sandbox','read-only','--model',self.args.model,
                 '--output-schema',windows_path(schema_path),
                 '--output-last-message',windows_path(output),'--json']
            for feature in ('shell_tool','unified_exec','multi_agent','apps','plugins',
                            'browser_use','computer_use','view_image'):
                cmd+=['--disable',feature]
            cmd+=['-']
            payload={k:body[k] for k in ('messages','tools','tool_choice') if k in body}
            proc=subprocess.run(cmd,input=PREFIX+json.dumps(payload,ensure_ascii=False),
                text=True,encoding='utf8',errors='replace',capture_output=True,
                cwd=where,timeout=self.args.timeout)
            usage={}; observed=[]
            for line in proc.stdout.splitlines():
                try: item=json.loads(line)
                except json.JSONDecodeError: continue
                kind=item.get('item',{}).get('type','')
                if kind: observed.append(kind)
                if kind in NATIVE_FORBIDDEN:
                    self.event({'event':'native_tool_rejected','call':count,'kind':kind})
                    raise ValueError('Native tool used outside FBS')
                if item.get('type')=='turn.completed': usage=item.get('usage',{})
            if proc.returncode or not output.exists():
                # Keep diagnostic text local; avoid logging SDK credential-bearing errors.
                self.event({'event':'call_failed','call':count,'returncode':proc.returncode,
                            'elapsed_sec':round(time.monotonic()-started,2)})
                raise RuntimeError('Subscription CLI failed; no model result')
            raw=output.read_text(encoding='utf8')
            try: answer=json.loads(raw)
            except ValueError:
                self.event({'event':'invalid_response_json','call':count,'bytes':len(raw)})
                raise
        tools={x['function']['name']:x['function'] for x in body.get('tools',[])}
        tool_calls=[]
        for call in answer['tool_calls']:
            if call['name'] not in tools: raise ValueError('Unknown function')
            if body.get('tool_choice')=='none': raise ValueError('Function call during wrap-up')
            arguments={}
            properties=tools[call['name']].get('parameters',{}).get('properties',{})
            for pair in call['arguments']:
                key,value=pair['name'],pair['value']
                if key in arguments or key not in properties:
                    raise ValueError('Duplicate or unknown parameter')
                arguments[key]=value if properties[key].get('type')=='string' else json.loads(value)
            required=tools[call['name']].get('parameters',{}).get('required',[])
            if not set(required)<=set(arguments): raise ValueError('Missing required function arguments')
            tool_calls.append({'id':'call_'+uuid.uuid4().hex,'type':'function',
                               'function':{'name':call['name'],'arguments':json.dumps(arguments,ensure_ascii=False)}})
        msg={'role':'assistant','content':answer['content']}
        if tool_calls: msg['tool_calls']=tool_calls
        prompt_tokens=usage.get('input_tokens',0); completion_tokens=usage.get('output_tokens',0)
        result={'id':'chatcmpl-'+uuid.uuid4().hex,'object':'chat.completion','created':int(time.time()),
                'model':self.args.model,'choices':[{'index':0,'message':msg,
                    'finish_reason':'tool_calls' if tool_calls else 'stop'}],
                'usage':{'prompt_tokens':prompt_tokens,'completion_tokens':completion_tokens,
                    'total_tokens':prompt_tokens+completion_tokens,
                    'prompt_tokens_details':{'cached_tokens':usage.get('cached_input_tokens',0)}}}
        self.event({'event':'call_completed','call':count,'model':self.args.model,
            'request_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),
            'elapsed_sec':round(time.monotonic()-started,2),'usage':usage,
            'native_item_types':sorted(set(observed)),'tools':[x['name'] for x in answer['tool_calls']],
            'content_chars':len(answer['content'])})
        print(json.dumps({'event':'subscription_call','call':count,'tools':len(tool_calls),
            'elapsed_sec':round(time.monotonic()-started,2)}),flush=True)
        return result

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,data):
        raw=json.dumps(data).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
        self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/health': return self.send(200,{'ok':True,'mode':'local-subscription','model':self.server.gateway.args.model})
        if self.path=='/v1/models' and self.headers.get('Authorization')=='Bearer '+self.server.gateway.key:
            return self.send(200,{'object':'list','data':[{'id':self.server.gateway.args.model,'object':'model'}]})
        return self.send(404,{'error':{'message':'not found'}})
    def do_POST(self):
        if self.headers.get('Authorization')!='Bearer '+self.server.gateway.key:
            return self.send(401,{'error':{'message':'unauthorized','type':'authentication_error'}})
        if self.path!='/v1/chat/completions': return self.send(404,{'error':{'message':'not found'}})
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=8*1024*1024: return self.send(413,{'error':{'message':'request too large'}})
        try:
            body=json.loads(self.rfile.read(size))
            with GATE: answer=self.server.gateway.complete(body)
            self.send(200,answer)
        except ValueError as exc:
            self.server.gateway.event({'event':'validation_error','error_type':type(exc).__name__,
                'message':str(exc)[:160]})
            self.send(503,{'error':{'message':'Subscription result validation failed; retry',
                'type':'server_error'}})
        except Exception as exc:
            self.server.gateway.event({'event':'gateway_error','error_type':type(exc).__name__})
            self.send(503,{'error':{'message':'Local subscription invocation failed','type':'server_error'}})

def main():
    p=argparse.ArgumentParser();p.add_argument('--bind',default='172.17.0.1');p.add_argument('--port',type=int,default=4017)
    p.add_argument('--model',default='gpt-6-astra');p.add_argument('--key-file',type=Path,required=True)
    p.add_argument('--scratch',type=Path,default=Path(tempfile.gettempdir())/'fbs-subscription-cli'); p.add_argument('--log',type=Path,required=True);p.add_argument('--max-calls',type=int,default=0)
    p.add_argument('--timeout',type=int,default=1800);a=p.parse_args()
    if a.bind not in ('127.0.0.1','172.17.0.1'): p.error('Bind only to loopback or the local Docker bridge')
    if not EXE: p.error('Codex CLI is not installed')
    a.scratch.mkdir(parents=True,exist_ok=True,mode=0o700)
    a.key_file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not a.key_file.exists():
        a.key_file.write_text(secrets.token_urlsafe(32));a.key_file.chmod(0o600)
    server=http.server.ThreadingHTTPServer((a.bind,a.port),Handler);server.gateway=Gateway(a)
    print(json.dumps({'event':'subscription_gateway_ready','bind':a.bind,'port':a.port,'model':a.model}),flush=True)
    server.serve_forever()

if __name__=='__main__': main()
