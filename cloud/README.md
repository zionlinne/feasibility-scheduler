# One public PLE task on GitHub-hosted Linux

**Current route: [88API setup and running instructions](API.md).** The text
below documents the previous subscription transport for earlier runs.

`ple-cloud.yml` runs the unchanged PR5 FBS runner through the same adapter used
locally. It uses the participant's Codex subscription as its model decision
engine; FBS executes the advertised tools through the official PLE protocol.
It does not replay a solution or have an operator supply model responses.

The cloud job installs pinned Codex CLI, uv, Harbor and the official PLE kit.
It first validates the Linux CLI argument shape without a model call. After
Codex authorization it requests one short subscription response before asking
for dataset login or building an engineering task. Failed invocations record
only a fixed diagnostic category (quota, login, configuration, schema, network,
runtime or unknown), never raw provider errors, task text or credentials.
It requests two official device authorizations: the ChatGPT subscription and
the Hugging Face account approved for the gated dataset. No paid model API key
is required. Authorize in the two named Actions steps. Ephemeral runners require
new logins on a new run; no subscription credential is copied from the desktop,
committed, cached or uploaded as an artifact.

The gateway binds to the local Docker bridge and uses a fresh local transport
token. Its CLI subprocesses have native tools disabled and return only FBS tool
decisions. Task tools run in the task container. Operator scratch files and
credential homes stay outside the checkout and the exported logs/artifacts.

After a run, download `ple-fbs-cloud-<run id>` for official results, exported
deliverables, serve logs and gateway usage metadata. A finished job is not by
itself proof of a usable submission: official artifact validation must pass.

GitHub-hosted jobs have a six-hour platform limit **including** installation,
login and build. The adapter adds no task/stage deadline, but a task needing its
entire official six-hour budget needs a longer-lived cloud machine. This job
runs one representative task, not the full 29-task suite.

The workflow is manually dispatchable; a change to `cloud/ple-start.json` on the
PR9 branch also starts it. It is never triggered by a fork pull request.
