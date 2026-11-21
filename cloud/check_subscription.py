"""One transport check, not a PLE solve or benchmark result."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.error
import urllib.request

from subscription_gateway import SCHEMA, cli_command, failure_category


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cli-only',action='store_true')
    args=parser.parse_args()
    if args.cli_only:
        with tempfile.TemporaryDirectory(prefix='fbs-cli-check-') as temp:
            root=Path(temp)
            schema=root/'schema.json';schema.write_text(json.dumps(SCHEMA))
            cmd=cli_command('gpt-6-astra','high',schema,root/'reply.json')
            result=subprocess.run(cmd[:-1]+['--help'],capture_output=True,text=True,timeout=30)
            if result.returncode:
                raise SystemExit('CLI invocation rejected: '+failure_category(result.stdout,result.stderr))
        print('CLI invocation accepted; no model call or login used')
        return
    root=Path(os.environ['PLE_OPERATOR'])
    token=(root/'gateway.key').read_text().strip()
    body={'model':'gpt-6-astra','reasoning_effort':'low',
          'messages':[{'role':'user','content':'Return SUBSCRIPTION_READY as content, with no function calls.'}]}
    request=urllib.request.Request('http://172.17.0.1:4017/v1/chat/completions',
        data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    try:
        with urllib.request.urlopen(request,timeout=1800) as response:answer=json.load(response)
    except urllib.error.HTTPError as exc:
        # The gateway returns only a fixed error category, never credential-bearing stderr.
        error=json.loads(exc.read()).get('error',{})
        raise SystemExit('Subscription transport check failed: '+str(error.get('type','unknown')))
    message=answer['choices'][0]['message']
    if message.get('tool_calls') or message.get('content','').strip()!='SUBSCRIPTION_READY':
        raise SystemExit('Subscription returned an invalid transport check response')
    print(json.dumps({'subscription_probe':'passed','model':'gpt-6-astra','engineering_trial':False}))


if __name__=='__main__':main()
