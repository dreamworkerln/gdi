import hashlib, json, os
from pathlib import Path
import subprocess, sys
from tests.test_ci_submission import OfflineSingleSubmissionTests
from gdi.exchange import decode, encode

runtime = Path(sys.argv[1]).resolve()
fixture = OfflineSingleSubmissionTests('test_offline_accept_needs_one_inbox_proof_and_no_job_snapshot')
try:
    fixture.setUp()
    worker = fixture.worker()
    _, publication, _ = fixture.ea.push('drive')
    snapshot = fixture.snapshot('prepare')
    capabilities = fixture.capabilities()
    fixture.plan = fixture.root / 'runtime-ci-plan'
    env = {key:value for key,value in os.environ.items() if not key.startswith('GIT_') and key not in ('PYTHONPATH', 'PYTHONHOME')}
    env['XDG_CONFIG_HOME'] = str(fixture.root / 'runtime-config')
    common = ['--repo', str(fixture.a.path), '--repository-id', fixture.identity]
    def call(operation, *arguments):
        result = subprocess.run([sys.executable, '-I', str(runtime), 'agent', 'ci', operation, *common, *map(str, arguments)],
                                cwd=fixture.root, env=env, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    plan = call('prepare', '--snapshot', snapshot, '--publication', publication, '--worker', 'user-host',
                '--profile', 'full', '--capabilities', capabilities, '--output', fixture.plan)
    assert set(plan['files']) == {'inbox'} and plan['safe_to_upload_inbox']
    fixture.transfer(plan, 'inbox')
    accepted = call('accept', '--snapshot', snapshot, '--plan', fixture.plan,
                    '--capabilities', capabilities, '--inbox-proof', fixture.proof(plan))
    assert accepted['state'] == 'accepted'
    pending = call('result', '--snapshot', snapshot, '--plan', fixture.plan)
    assert pending['state'] == 'PENDING' and not pending['verified']
    worker.tick()
    complete = fixture.snapshot('complete', plan['job_id'], True)
    document = decode(complete.read_bytes())
    document['files'] = [item for item in document['files'] if not item['local_path'].endswith('/submission.json')]
    complete.write_bytes(encode(document))
    result = call('result', '--snapshot', complete, '--plan', fixture.plan)
    assert result['state'] == 'PASS' and result['verified']
    assert fixture.counter.read_text() == 'x'
    print(json.dumps({'runtime_sha256':hashlib.sha256(runtime.read_bytes()).hexdigest(),
                      'upload_files':list(plan['files']), 'accept':accepted['state'], 'before_worker':pending['state'],
                      'result':result['state'], 'verified':result['verified'], 'executions':1,
                      'submission_download_required':False}, sort_keys=True))
finally:
    fixture.doCleanups()
    fixture.tearDown()
