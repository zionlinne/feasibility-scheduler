"""Watch the single-task runner's serve container and print public progress only."""

import json
import math
import re
import signal
import subprocess
import time


# Container labels and names are from the pinned PLE kit. The Actions runner
# runs one task at a time, so there is only one relevant serve container.
SERVE_NAME = re.compile(r'plebench-serve-[0-9a-f]{12}')
PHASES = {'plan': 'Planning', 'import': 'Delivery import', 'worker': 'Worker', 'root': 'Root'}


def count(value):
    return value if type(value) is int and value >= 0 else 0


class Progress:
    def __init__(self):
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0

    def summary(self, line):
        # Never print the raw line or any model-supplied text.
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get('event')
        if kind == 'pr5_runner_started':
            return f'FBS started (up to {count(event.get("contract_repair_limit"))} repairs)'
        if kind == 'pr5_task_io_import_started':
            return 'Identifying deliverables'
        if kind == 'pr5_native_dispatch':
            if event.get('planning') is True:
                return 'Contract planning dispatched'
            repair_round = count(event.get('repair_round'))
            if repair_round:
                scope = 'Composition' if event.get('task_id') == '__contract_composition__' else 'Worker'
                return f'{scope} repair {repair_round} dispatched'
            return 'Worker dispatched'
        if kind == 'pr5_first_complete_delivery':
            return 'First complete package detected on disk'
        if kind in ('pr5_root_review_started', 'pr5_root_review_finished'):
            review = f'Root review {count(event.get("round"))}'
            if kind == 'pr5_root_review_started':
                return f'{review} started'
            status = event.get('status')
            if status in ('passed', 'failed', 'unverified'):
                return f'{review} finished ({status})'
            return None
        if kind == 'pr5_finished' and type(event.get('ok')) is bool:
            return f'FBS finished (accepted={event["ok"]})'

        phase = event.get('phase')
        if not isinstance(phase, str) or phase not in PHASES:
            return None
        phase = PHASES[phase]
        if kind == 'pr5_model_call_started':
            return f'{phase} API call started'
        if kind not in ('pr5_model_stream_received', 'pr5_model_call_finished', 'pr5_model_call_failed'):
            return None
        seconds = event.get('seconds')
        if type(seconds) not in (int, float) or not 0 <= seconds < math.inf:
            return None
        if kind == 'pr5_model_stream_received':
            return f'{phase} API stream receiving data at {seconds:.1f}s'
        if kind == 'pr5_model_call_failed':
            return f'{phase} API call failed after {seconds:.1f}s'
        input_tokens = count(event.get('input_tokens'))
        self.input_tokens += input_tokens
        self.cached_tokens += min(input_tokens, count(event.get('cached_tokens')))
        self.output_tokens += count(event.get('output_tokens'))
        hit_rate = self.cached_tokens / self.input_tokens if self.input_tokens else 0
        return (f'{phase} API call returned in {seconds:.1f}s | observed total: '
                f'{self.input_tokens:,} input, {self.output_tokens:,} output, {hit_rate:.1%} cache hit')


def follow():
    progress = Progress()
    seen = set()
    start = time.monotonic()
    while True:
        containers = subprocess.run(
            ['docker', 'ps', '-a', '--filter', 'label=plebench.run', '--format', '{{.Names}}'],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.splitlines()
        for name in containers:
            if name in seen or not SERVE_NAME.fullmatch(name):
                continue
            seen.add(name)
            with subprocess.Popen(
                ['docker', 'logs', '--follow', name], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', errors='replace',
            ) as stream:
                try:
                    for line in stream.stdout:
                        summary = progress.summary(line)
                        if summary:
                            elapsed = int(time.monotonic() - start)
                            print(f'[FBS +{elapsed // 60:02d}:{elapsed % 60:02d}] {summary}', flush=True)
                finally:
                    if stream.poll() is None:
                        stream.kill()
        time.sleep(2)


def stop(signum, frame):
    raise SystemExit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        follow()
    except (OSError, subprocess.SubprocessError):
        print('[FBS] Live progress unavailable; use the evaluation and exported logs.', flush=True)
