# Current cloud API route

The API workflow replaces the subscription workflow described in the historical
README. `submission.yaml` declares `openai-compatible/gpt-6.1-sol` at
`https://88api.ai/v1`. The workflow no longer invokes Codex subscription login
or the subscription gateway. PR5 planning, scheduling, acceptance and repair
are unchanged.

In repository Settings > Secrets and variables > Actions, add:

- `FBS_MODEL_API_KEY`: your 88API key with access to the declared model.
- `HF_TOKEN` (optional): a read token from the Hugging Face account approved
  for `google/physical-engineerings-last-exam`. Without it, each new runner
  requests the official Hugging Face device login.

The key is checked before installation. It goes into a private temporary env
file outside the checkout, excluded from artifact uploads and removed at the
end. No credential is committed or copied from the desktop.

Before downloading the task, two short API requests verify an actual tool call,
the tool-result round trip and final JSON. The pinned official kit parses the
model/endpoint from the real submission. The probe uses the same high reasoning
and completion-token parameter as the adapter with a small output allowance.
It logs only status, token counts and error type/HTTP status, excluding provider
error bodies and credentials. A failed probe prevents the engineering run.

After secrets are configured, start the workflow on the PR9 branch manually or
update `cloud/ple-start.json`. Changing only API code or the manifest does not
launch a task. This workflow is not triggered by fork PRs.

The representative run uses `frame-extension-method`, one trial and one
infrastructure attempt. Download `ple-fbs-cloud-<run id>` for official scores,
actual deliverables, serve logs and API probe metadata. Job success, FBS final
acceptance and official score are separate outcomes. This does not submit to
the competition or run all 29 tasks.

GitHub's six-hour platform limit includes setup and login. The adapter adds no
stage deadline or model-call limit. A full six-hour task budget needs a host
with a longer platform limit.
