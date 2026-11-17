# Current cloud API route

The API workflow replaces the subscription workflow described in the historical
README. `submission.yaml` declares `openai-compatible/gpt-6.1-sol` at
`https://88api.ai/v1`. The workflow no longer invokes Codex subscription login
or the subscription gateway. PR5 planning, scheduling, acceptance and repair
are unchanged.

No repository Secrets are used. Each run requests one encrypted credential
upload. The runner creates a fresh RSA-3072 private key and exports only
`api-request.json` in `ple-api-request-<run id>`. Download that public request
and open the local PowerShell 7 `enter-api-key.ps1` with `-RequestPath` and
`-OutputPath`. Enter your 88API key in the hidden prompt. The helper writes
only RSA-OAEP-SHA256 ciphertext; the plain key is not written to a local file.

Upload that encrypted JSON as `api-upload.json` on the temporary branch named
in the public request (`runtime/ple-api-<run id>`). The operator can do this
through the connected GitHub tools; it never needs to read the plain key.
The cloud job polls that exact branch using its built-in read-only GitHub
token, verifies the run ID and public-key fingerprint, decrypts the key into
a private temporary model env file and immediately removes the private RSA
key. Each new run has a different key pair; old ciphertext cannot unlock it.
The encrypted transport branch may remain, but contains no plain credential.

The private model env file stays outside the checkout, is excluded from
artifact uploads and is removed when the task ends. No repository API Secret,
desktop credential copy or account-token persistence is needed. Hugging Face
still requests its official approved-account device login on a new runner.

Before downloading the task, two short API requests verify an actual tool call,
the tool-result round trip and final JSON. The pinned official kit parses the
model/endpoint from the real submission. The probe uses the same high reasoning
and completion-token parameter as the adapter with a small output allowance.
It logs only status, token counts and error type/HTTP status, excluding provider
error bodies and credentials. A failed probe prevents the engineering run.

Start the workflow on the PR9 branch manually or update `cloud/ple-start.json`.
It waits for that run's encrypted upload before installing tools or calling a
model. Changing only API code or the manifest does not launch a task. This
workflow is not triggered by fork PRs.

The representative run uses `frame-extension-method`, one trial and one
infrastructure attempt. Download `ple-fbs-cloud-<run id>` for official scores,
actual deliverables, serve logs and API probe metadata. Job success, FBS final
acceptance and official score are separate outcomes. This does not submit to
the competition or run all 29 tasks.

GitHub's six-hour platform limit includes setup and login. The adapter adds no
stage deadline or model-call limit. A full six-hour task budget needs a host
with a longer platform limit.
