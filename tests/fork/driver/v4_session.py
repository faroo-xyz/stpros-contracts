"""The V4 scenario of the real-state S3 session (``s3_run.main``): the deployed-topology release flow and A-G.

The reviewed session (``streamlined-upgrade-readiness/s3-execution/scripts/s3_run.py``) keeps doing everything around
it: record loading, the S1/S2 anchor binding, ``real_state.topology`` from the content-verified S1 archive, the budget
ledger, the proxy-only launcher, the legacy identity reads, the preflight and the snapshot per scenario. This module only
provides the scenario it calls (``scenario``), with:
- the release history segments taken from the very S1 manifest the topology verified (bound by its SHA-256);
- the reviewed governance addresses (``distributor_predeploy.governance_inputs``), the GOV Safe checked against the
  topology's;
- ``v4_fork.DEPLOYED`` (the deployed P-0, pre-deployment and ReleaseAssertions) and ``v4_fork.REAL_INPUTS`` (rev 2 and
  the three real SLPs);
- ``v4_fork.run_release`` with its gates, then ``v4_acceptance.acceptance`` only after H / U / R4 PASS.
The scenario result has the ledger shape ``s3_run.summarize`` reads. Test-only; nothing here reaches a public endpoint
by itself (the node it gets is the session's forked node behind the bounded proxy).
"""
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import history as h  # noqa: E402
from localchain import ExecutionError  # noqa: E402
from tools.release.native import distributor_predeploy as dp  # noqa: E402

SCENARIO = 'V4.release'
LAST_EVIDENCE = None            # {path, sha256} of the last complete run document (read by the session CLI)


def archive_segments(history_root, run_id, manifest_sha256):
    """The release-proof segments (absolute paths) of the S1 manifest the topology verified; refuses another one."""
    root = Path(history_root)
    raw = (root / run_id / 'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ExecutionError('V4_SESSION: the S1 manifest is not the one the topology verified')
    manifest = json.loads(raw)
    return [{'planFile': str(root / s['planFile']), 'planSha256': s['planSha256'],
             'chunks': [{'path': str(root / c['path']), 'sha256': c['sha256']} for c in s['chunks']]}
            for s in manifest['segments']], manifest


def real_actors(meta, inputs):
    """The reviewed mainnet governance (GOV = OPS Safe, CAN, Guardian, deployer) and the rev 2 operational actors."""
    g = dp.governance_inputs()
    if meta['safe'].lower() != g['gov']:
        raise ExecutionError(f'V4_SESSION: the topology GOV Safe {meta["safe"]} is not the reviewed GOV {g["gov"]}')
    return {'gov': g['gov'], 'ops': g['ops'], 'can': g['can'], 'guardian': g['guardian'], 'deployer': g['deployer'],
            'treasury': inputs['treasury'], 'reporter': inputs['reporter'], 'keeper': inputs['keeper']}


def ledger_of(doc):
    """The gate document as ``s3_run.summarize`` reads it: one check per gate and per A-G verdict."""
    checks = [{'id': 'identity', 'verdict': 'PASS' if doc.get('identity') and all(r['ok'] for r in doc['identity']) else 'FAIL',
               'expected': 'deployed runtimes', 'actual': [r['id'] for r in doc.get('identity', []) if not r['ok']][:8],
               'detail': None}]
    for stage in ('preflight', 'p1-runbook', 'build-handoff', 'build-u'):
        if stage in doc['stages']:
            checks.append({'id': stage, 'verdict': 'PASS', 'expected': 'PASS', 'actual': 'PASS', 'detail': None})
    for stage in ('H', 'U'):
        if stage in doc['stages']:
            s = doc['stages'][stage]
            checks.append({'id': stage, 'verdict': 'PASS' if s['ok'] else 'FAIL', 'expected': 'status 1 and Safe inner success',
                           'actual': {'status': s['status'], 'gasUsed': s['gasUsed']}, 'detail': s['transactionHash']})
    for stage in ('Hcheckpoint', 'R4'):
        if stage in doc['stages']:
            s = doc['stages'][stage]
            checks.append({'id': stage, 'verdict': 'PASS' if s['status'] == 'PASS' else 'FAIL', 'expected': 'PASS',
                           'actual': s['status'], 'detail': s.get('error')})
    if 'UbeforeDelay' in doc['stages']:
        checks.append({'id': 'UbeforeDelay', 'verdict': 'PASS' if doc['stages']['UbeforeDelay']['refused'] else 'FAIL',
                       'expected': 'refused', 'actual': doc['stages']['UbeforeDelay'], 'detail': None})
    if 'Ugas' in doc['stages']:
        g = doc['stages']['Ugas']
        checks.append({'id': 'Ugas', 'verdict': 'PASS' if g['ok'] else 'FAIL', 'expected': f'<= {g["cap"]}',
                       'actual': g['gasUsed'], 'detail': None})
    for cid, case in sorted((doc.get('acceptance') or {}).items()):
        checks.append({'id': f'acceptance.{cid}', 'verdict': case['verdict'], 'expected': 'PASS',
                       'actual': {'evidence': case['evidence'], 'notApplicable': case.get('notApplicable')},
                       'detail': case.get('blocked')})
    for stage in doc.get('notRun', []):
        checks.append({'id': stage, 'verdict': 'NOT RUN', 'expected': None, 'actual': None,
                       'detail': f'stopped at {doc["stoppedAt"]}' if doc.get('stoppedAt') else None})
    checks += [{'id': 'UPGRADE', 'verdict': doc['upgradeVerdict'], 'expected': 'PASS', 'actual': doc['releaseGate'],
                'detail': 'H -> H checkpoint -> 600 s -> U -> gas cap -> R4'},
               {'id': 'BUSINESS', 'verdict': doc['businessVerdict'], 'expected': 'PASS',
                'actual': {'cases': doc['caseVerdicts'], 'missingCases': doc['missingCases']},
                'detail': 'A-G (G accounting and G-INTEGRATION separately); every required case must be present'},
               {'id': 'authorization', 'verdict': 'INFO', 'expected': None, 'actual': None, 'detail': doc['authorization']}]
    if doc.get('caseVerdicts'):                                # A-G ran: a required case it did not return is MISSING
        checks += [{'id': f'acceptance.{cid}', 'verdict': 'MISSING', 'expected': 'PASS', 'actual': None,
                    'detail': 'required case not returned'} for cid in doc['missingCases']]
    if doc.get('evidence'):
        checks.append({'id': 'evidence.fullRecord', 'verdict': 'INFO', 'expected': None, 'actual': doc['evidence'],
                       'detail': 'the complete run document (stages, receipts, A-G rows and receipts, transcript)'})
    # the ledger verdict is OVERALL: it never shows PASS over a FAIL, BLOCKED, PARTIAL or an incomplete run
    return {'aggregate': doc['overallVerdict'], 'verdict': doc['overallVerdict'], 'upgradeVerdict': doc['upgradeVerdict'],
            'businessVerdict': doc['businessVerdict'], 'missingCases': doc['missingCases'], 'checks': checks}


def scenario(node, topo, history_root, run_id, *, name='real-mainnet-v4', synthetic=None):
    """The V4 release flow and A-G on the session's forked node. Returns the ``s3_run`` result shape plus the full
    gate document (``v4``). ``synthetic`` is for the SIMULATION wiring test only (``test_v4_session``): its deployed
    descriptor, actors, inputs, created start and RPC wrapper replace the real ones; a real session never passes it."""
    import execution as ex
    import v4_acceptance as va
    import v4_fork as vf
    run = ex.Run(topo, name, node)
    try:
        segments, manifest = archive_segments(history_root, run_id, topo['binding']['s1ManifestSha256'])
        s = synthetic or {}
        inputs = s.get('inputs') or dict(vf.REAL_INPUTS)
        created_start = s.get('createdStart') or h.B0_PREFIX['mainnet']['end'] + 1
        doc = vf.run_release(run, s.get('deployed') or vf.DEPLOYED, s.get('actors') or real_actors(run.m, inputs), inputs,
                             history_segments=segments, created_start=created_start,
                             history_from_block=manifest['fromBlock'], then=va.acceptance, rpc_factory=s.get('rpcFactory'))
        doc['binding'] = {'forkBlock': topo['binding']['forkBlock'], 'forkBlockHash': topo['binding']['forkBlockHash'],
                          's1ManifestSha256': topo['binding']['s1ManifestSha256'],
                          'executionSource': topo['binding'].get('executionSource'),
                          'historyFromBlock': manifest['fromBlock'], 'createdStart': created_start,
                          'class': 'SYNTHETIC SIMULATION' if synthetic else 'REAL-STATE FORK'}
        # the complete run document (with the binding) is the raw record; the ledger only points to it by SHA-256
        full = run.dir / 'v4-fork-run.json'
        full.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
        doc['evidence'] = {'path': str(full), 'sha256': hashlib.sha256(full.read_bytes()).hexdigest()}
        global LAST_EVIDENCE
        LAST_EVIDENCE = doc['evidence']
        return {'run': name, 'ledgers': {SCENARIO: ledger_of(doc)}, 'transcript': run.chain.transcript, 'v4': doc}
    finally:
        run.close()
