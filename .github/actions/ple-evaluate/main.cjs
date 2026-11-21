const { spawn } = require('node:child_process');
const { copyFileSync, existsSync } = require('node:fs');
const { join } = require('node:path');

async function main() {
  const operator = process.env.PLE_OPERATOR;
  const { DefaultArtifactClient } = require(join(operator, 'artifact-client/node_modules/@actions/artifact'));
  const artifacts = new DefaultArtifactClient();
  const snapshot = join(operator, 'first-delivery.zip');
  const log = join(operator, 'live-progress.log');
  const savedLog = join(operator, 'checkpoint-progress.log');
  const name = `ple-fbs-first-delivery-${process.env.GITHUB_RUN_ID}-${process.env.GITHUB_RUN_ATTEMPT}`;
  let pending;
  let uploaded = false;
  let nextAttempt = 0;

  function uploadAvailable(force = false) {
    if (pending || uploaded || !existsSync(snapshot) || (!force && Date.now() < nextAttempt)) {
      return pending;
    }
    nextAttempt = Date.now() + 30000;
    pending = (async () => {
      const files = [snapshot];
      if (existsSync(log)) {
        copyFileSync(log, savedLog);
        files.push(savedLog);
      }
      const result = await artifacts.uploadArtifact(name, files, operator, { retentionDays: 7, compressionLevel: 0 });
      uploaded = true;
      console.log(`[FBS] First complete ZIP uploaded as ${name} (artifact ${result.id}).`);
    })().catch(() => {
      console.log('::warning::First-delivery upload did not finish; the runner still has its local copy.');
    }).finally(() => { pending = undefined; });
    return pending;
  }

  const runnerEnv = { ...process.env };
  for (const name of ['ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RUNTIME_URL', 'ACTIONS_RESULTS_URL', 'ACTIONS_CACHE_URL']) {
    delete runnerEnv[name];
  }
  const runner = spawn('bash', [join(process.env.GITHUB_WORKSPACE, 'cloud/run_eval.sh')], {
    detached: true,
    env: runnerEnv,
    stdio: 'inherit',
  });
  const forward = (signal) => {
    if (runner.pid && runner.exitCode === null && runner.signalCode === null) {
      try { process.kill(-runner.pid, signal); } catch (error) {
        if (error.code !== 'ESRCH') throw error;
      }
    }
  };
  process.on('SIGINT', forward);
  process.on('SIGTERM', forward);
  const timer = setInterval(uploadAvailable, 1000);
  try {
    process.exitCode = await new Promise((resolve, reject) => {
      runner.once('error', reject);
      runner.once('close', code => resolve(code ?? 1));
    });
  } finally {
    clearInterval(timer);
    await uploadAvailable(true);
    process.off('SIGINT', forward);
    process.off('SIGTERM', forward);
  }
}

main().catch(() => {
  console.error('::error::The PLE evaluation action could not run.');
  process.exitCode = 1;
});
