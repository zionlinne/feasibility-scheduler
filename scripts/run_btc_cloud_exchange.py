"""Run unmodified PR8 on GitHub Actions; bridge live file channels to GitHub.

The assistant authors reports and reviews after reading each emitted request.
This bridge neither writes reports nor fabricates acceptance judgments.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ['GITHUB_REPOSITORY']
BRANCH = os.environ['GITHUB_REF_NAME']
PREFIX = 'cloud/btc2026'
RUN = Path('cloud-run')
TOKEN = os.environ['GH_TOKEN']

def api(path, payload=None):
    raw = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request('https://api.github.com/repos/' + REPO + '/' + path,
        data=raw, method='GET' if payload is None else 'PUT', headers={
        'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and payload is None:
            return None
        raise

def get(path):
    record = api('contents/' + path + '?ref=' + urllib.parse.quote(BRANCH, safe=''))
    return None if record is None else base64.b64decode(record['content'])

def put(path, data):
    record = api('contents/' + path + '?ref=' + urllib.parse.quote(BRANCH, safe=''))
    payload = {'branch': BRANCH, 'message': 'Record live BTC FBS exchange ' + path,
               'content': base64.b64encode(data).decode()}
    if record:
        if base64.b64decode(record['content']) == data:
            return
        payload['sha'] = record['sha']
    api('contents/' + path, payload)

run_meta = {'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
    'execution': 'GitHub Actions ubuntu-latest', 'fbs_commit': '13c8c04b65cd0b954eeab81f79fb4799c1316780',
    'author_and_root': 'Current assistant through live GitHub file exchange; not independent agents',
    'research_cutoff': '2026-11-17 Asia/Shanghai; source-specific timestamps retained'}
put(PREFIX + '/live/runner.json', json.dumps(run_meta, indent=2).encode())
env = dict(os.environ, PYTHONPATH='src')
log = open('cloud-console.log', 'w')
process = subprocess.Popen([sys.executable, 'scripts/run_exploration_experiment.py',
    PREFIX + '/plan.json', str(RUN), '--timeout', '2400'], env=env, stdout=log, stderr=subprocess.STDOUT)
published = set()
deadline = time.monotonic() + 3100
try:
    while process.poll() is None:
        if time.monotonic() > deadline:
            process.terminate()
            raise TimeoutError('Cloud exchange total deadline elapsed')
        for channel in ('requests', 'root_requests'):
            for path in sorted((RUN / channel).glob('*.json')):
                key = str(path.relative_to(RUN))
                if key not in published:
                    put(PREFIX + '/live/' + key, path.read_bytes())
                    published.add(key)
                    print('Published', key, flush=True)
        for channel in ('artifacts', 'root_reviews'):
            directory = RUN / channel
            if not directory.exists():
                continue
            request_channel = 'requests' if channel == 'artifacts' else 'root_requests'
            for request in sorted((RUN / request_channel).glob('*.json')):
                target = directory / request.name
                if target.exists():
                    continue
                data = get(PREFIX + '/deliveries/' + channel + '/' + request.name)
                if data is not None:
                    json.loads(data)
                    temp = target.with_suffix('.tmp')
                    temp.write_bytes(data)
                    temp.replace(target)
                    print('Delivered', channel, request.name, flush=True)
        time.sleep(5)
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=20)
    log.close()
    if RUN.exists():
        (RUN / 'runner.json').write_text(json.dumps(run_meta, indent=2))
        for path in sorted(RUN.rglob('*')):
            if path.is_file() and path.suffix in ('.json', '.jsonl'):
                put(PREFIX + '/result/' + str(path.relative_to(RUN)), path.read_bytes())
        for path in (RUN / 'artifacts').glob('*.json'):
            report = json.loads(path.read_text())['artifact'].get('report', '')
            if report:
                put(PREFIX + '/reports/' + path.stem + '.md', report.encode())
    put(PREFIX + '/result/console.txt', Path('cloud-console.log').read_bytes())
sys.exit(process.returncode)