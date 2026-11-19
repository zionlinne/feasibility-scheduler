"""Two small model calls verify JSON object mode and tool transport before an engineering run.

Credentials come only from the environment; provider text is never logged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request


def settings(entry):
    # Use the actual official parser, not a separate model/base URL setting.
    from kit import spec
    return spec.load(Path(entry))


def probe(sub, key, *, request=urllib.request.urlopen, stream_client=None):
    if not key or any(c in key for c in '\r\n\x00'):
        raise ValueError('MODEL_API_KEY is missing or invalid')
    model=sub.model.name
    endpoint=sub.model.endpoint.rstrip('/')+'/chat/completions'
    messages=[{'role':'user','content':'Call api_echo with value API_READY, then return exactly {"status":"API_READY"}.'}]
    function={'name':'api_echo','description':'Echo the supplied probe string.',
              'parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}
    usage={'input':0,'output':0}

    def ask(**options):
        body={'model':model,'messages':messages,'reasoning_effort':'high',
              'max_completion_tokens':4096,**options}
        if stream_client is None:
            req=urllib.request.Request(endpoint,json.dumps(body).encode(),
                {'Content-Type':'application/json','Authorization':'Bearer '+key})
            with request(req,timeout=180) as reply:
                data=json.load(reply)
        else:
            with stream_client.chat.completions.stream(**body, stream_options={'include_usage': True}) as stream:
                response=stream.get_final_completion()
            if not response.choices or not response.choices[0].finish_reason:
                raise ValueError('Model stream ended before the completion finished')
            if response.usage is None:
                raise ValueError('Model stream did not include token usage')
            data=response.model_dump(exclude_none=True)
        for total,field in [('input','prompt_tokens'),('output','completion_tokens')]:
            usage[total]+=data.get('usage',{}).get(field,0) or 0
        return data['choices'][0]['message']

    first=ask(tools=[{'type':'function','function':function}],
              tool_choice={'type':'function','function':{'name':'api_echo'}})
    calls=first.get('tool_calls',[])
    if len(calls)!=1 or calls[0]['function']['name']!='api_echo':
        raise ValueError('API did not return the required tool call')
    if json.loads(calls[0]['function']['arguments'])!={'value':'API_READY'}:
        raise ValueError('API tool arguments did not match the probe')
    messages.append(first)
    messages.append({'role':'tool','tool_call_id':calls[0]['id'],
                     'content':json.dumps({'value':'API_READY'})})
    last=ask(response_format={'type':'json_object'})
    if last.get('tool_calls') or json.loads(last.get('content') or '')!={'status':'API_READY'}:
        raise ValueError('API did not return the required final JSON')
    return {'ok':True,'model':model,'calls':2,'tokens':usage,'engineering_trial':False}


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('entry')
    parser.add_argument('--env-file',type=Path)
    args=parser.parse_args()
    try:
        key=os.environ.get('MODEL_API_KEY','')
        if args.env_file:
            text=args.env_file.read_text(encoding='utf-8')
            if not text.startswith('MODEL_API_KEY=') or text.count('\n')!=1 or not text.endswith('\n'):
                raise ValueError('Invalid private model env file')
            key=text[len('MODEL_API_KEY='):-1]
        sub=settings(args.entry)
        if getattr(sub, 'env', {}).get('FBS_PLE_MODEL_STREAM') == '1':
            from openai import OpenAI
            with OpenAI(api_key=key,base_url=sub.model.endpoint,timeout=180,max_retries=0) as client:
                result=probe(sub,key,stream_client=client)
            result['stream']=True
        else:
            result=probe(sub,key)
    except Exception as exc:
        # Do not expose a provider response body, URL, request headers or key.
        result={'ok':False,'error_type':type(exc).__name__}
        if isinstance(exc,urllib.error.HTTPError): result['http_status']=exc.code
        elif type(getattr(exc,'status_code',None)) is int: result['http_status']=exc.status_code
    print(json.dumps(result),flush=True)
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
