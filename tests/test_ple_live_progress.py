import json
import runpy
from pathlib import Path


Progress = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'cloud/live_progress.py'))['Progress']


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


def test_model_cache_totals_use_input_weighted_rate():
    progress = Progress()
    for tokens, cached in [(100, 0), (900, 900)]:
        summary = progress.summary(json.dumps({
            'event': 'pr5_model_call_finished', 'phase': 'worker', 'seconds': 1.5,
            'input_tokens': tokens, 'cached_tokens': cached, 'output_tokens': 50,
        }))
    assert summary == 'Worker API call returned in 1.5s | observed total: 1,000 input, 100 output, 90.0% cache hit'
