import json
import runpy
from types import SimpleNamespace
from pathlib import Path
from zipfile import ZipFile


progress_module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'cloud/live_progress.py'))
Progress = progress_module['Progress']


def test_public_progress_only_emits_allowed_summary_fields():
    progress = Progress()
    assert progress.summary('raw provider error with secret') is None
    assert progress.summary(json.dumps({
        'event': 'pr5_root_review_finished', 'round': 2, 'status': 'failed',
        'detail': 'private task content', 'rejected_criteria': [{'evidence': 'private material'}],
    })) == 'Root review 2 finished (failed)'
    assert progress.summary(json.dumps({
        'event': 'pr5_native_dispatch', 'task_id': '__contract_composition__', 'repair_round': 3,
    })) == 'Composition repair 3 dispatched'
    assert progress.summary(json.dumps({
        'event': 'pr5_model_stream_received', 'phase': 'worker', 'seconds': 10,
        'content': 'private fragment',
    })) == 'Worker API stream receiving data at 10.0s'
    assert progress.summary(json.dumps({
        'event': 'pr5_model_phase_finished', 'phase': 'root', 'seconds': 21.5,
        'calls': 6, 'tools': 9, 'content': 'private fragment',
    })) == 'Root finished in 21.5s | 6 API calls, 9 tool calls'


def test_model_cache_totals_use_input_weighted_rate():
    progress = Progress()
    for tokens, cached in [(100, 0), (900, 900)]:
        summary = progress.summary(json.dumps({
            'event': 'pr5_model_call_finished', 'phase': 'worker', 'seconds': 1.5,
            'input_tokens': tokens, 'cached_tokens': cached, 'output_tokens': 50,
        }))
    assert summary == 'Worker API call returned in 1.5s | observed total: 1,000 input, 100 output, 90.0% cache hit'


def test_snapshot_copy_keeps_first_valid_archive(tmp_path, monkeypatch):
    copy = progress_module['copy_snapshot']
    candidate = tmp_path/'fixture.zip'
    with ZipFile(candidate, 'w') as archive:
        archive.writestr('submission.zip', b'first')
    calls = []

    def docker(args, *, stdout, **options):
        calls.append(args)
        stdout.write(b'partial' if len(calls) == 1 else candidate.read_bytes())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(progress_module['subprocess'], 'run', docker)
    target = tmp_path/'first-delivery.zip'
    container = 'plebench-serve-012345abcdef'
    assert not copy(container, tmp_path) and not target.exists()
    assert copy(container, tmp_path)
    assert copy(container, tmp_path)
    assert len(calls) == 2 and calls[-1] == ['docker', 'exec', container, 'cat', '/tmp/fbs-first-delivery.zip']
    assert target.read_bytes() == candidate.read_bytes()
