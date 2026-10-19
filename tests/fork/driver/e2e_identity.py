"""Formal inputs of the post-upgrade E2E gate: approved release record, hash bindings, Fork-state identity checks,
Safe reads, history authentication and the upgrade postcheck. Test-only.

Nothing here can approve itself:
- A release record counts only if its raw SHA-256 is pinned in ``APPROVED_E2E_RECORDS``, in reviewed source. The pin
  set is empty today.
- Every Safe manifest it names must be pinned in ``campaign_input.APPROVED_SAFE_IDENTITY_MANIFESTS``. That set is also
  empty today.
- Every identity it names is compared with the current state of the Fork: Timelock roles, guardians, keeper,
  reporter, treasury, the governed vault's OPS, and each Safe's owners, threshold, modules and guard.
- History counts as authenticated only if all of these hold:
  - the run topology itself says so;
  - the final provenance gate is implemented (``scenarios.FINAL_GATE_PROVENANCE_IMPLEMENTED``, False today);
  - the approved record binds the same S1 manifest.

  Nothing in this module lowers FH-D2.
"""
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
import safe_governance as sg  # noqa: E402

RECORD_SCHEMA = 'faroo-e2e-release-record-v1'
APPROVED_E2E_RECORDS = frozenset()          # raw SHA-256 of independently approved records (reviewed-source pin)
FROZEN_COMMIT = 'a110037aa915a1dcc6fad8379ae4aa88595f72a4'
ROLE_KEYS = ('gov', 'can', 'guardian', 'ops', 'keeper', 'reporter', 'treasury')
SAFE_ROLES = ('gov', 'can', 'guardian', 'ops', 'treasury')
TL_ROLES = {'PROPOSER': '0x' + keccak256(b'PROPOSER_ROLE')[2:], 'EXECUTOR': '0x' + keccak256(b'EXECUTOR_ROLE')[2:],
            'CANCELLER': '0x' + keccak256(b'CANCELLER_ROLE')[2:]}
OPERATOR = '0x' + keccak256(b'OPERATOR_ROLE')[2:]
SAFE_GUARD_SLOT = '0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8'
SENTINEL = '0x' + '00' * 19 + '01'
# The upgrade transaction and the checks that must exist after it, per chain and governance route.
POSTCHECK = {
    688689: {'tx': 'U_t.executeBatch', 'checks': ('tx.U_t.executeBatch', 'GC-03.afterU_t.doneNoPending'), 'postMin': 20},
    (688689, 'SAFE'): {'tx': 'U_t.execTransaction', 'postMin': 20, 'checks': (
        'SAFE.gov.identity==approved', 'SAFE.can.identity==approved', 'SAFE.policy.signerIsolation',
        'T-5.scheduleBatch.executionSuccess', 'T-5.scheduleBatch.innerEffect', 'T-5.scheduleBatch.outer==inner',
        'U_t.safeTxHash==independentEIP712', 'U_t.signers.validOrderedThreshold', 'U_t.approvalsRecorded',
        'U_t.executionSuccess', 'U_t.outer==inner', 'U_t.innerEffect', 'U_t.nonceAdvanced', 'SAFE.gov.nonce.afterU_t',
        'GC-03.afterU_t.doneNoPending')},
    1672: {'tx': 'U.execTransaction', 'checks': ('U.innerSuccess', 'GC-03.afterU.doneNoPending'), 'postMin': 25},
}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _lower(a):
    return a.lower() if isinstance(a, str) else a


# ------------------------------------------------------------------------------------------- release record
def load_record(path, expected_sha256):
    """(record or None, problems). Every problem is a BLOCKED reason; an unpinned record is never used."""
    problems = []
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        return None, [f'BLOCKED record: cannot read {path}: {error}']
    sha = hashlib.sha256(raw).hexdigest()
    if sha != expected_sha256:
        return None, [f'BLOCKED record: raw SHA-256 {sha[:16]} is not the expected {str(expected_sha256)[:16]}']
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, ['BLOCKED record: not JSON']
    if doc.get('schema') != RECORD_SCHEMA:
        return None, ['BLOCKED record: schema']
    if sha not in APPROVED_E2E_RECORDS:
        problems.append('BLOCKED record: not independently approved (raw SHA-256 not pinned in APPROVED_E2E_RECORDS)')
    for key in ('chainId', 'anchor', 'binding', 'actors', 'safes', 'history', 'readAuthorization', 'ownerInputs', 'integrations'):
        if key not in doc:
            problems.append(f'BLOCKED record: field {key} missing')
    return (None, problems) if problems else (doc, [])


def binding_problems(record, spec_sha256, spec, canonical_hashes):
    """Integrity mismatches between the approved record and what this run executes (each one is a FAIL)."""
    b, out = record['binding'], []
    if b.get('frozenCommit') != FROZEN_COMMIT:
        out.append(f'FAIL binding: record frozen commit {b.get("frozenCommit")} is not {FROZEN_COMMIT}')
    if b.get('specSha256') != spec_sha256:
        out.append(f'FAIL binding: E2E spec {spec_sha256[:16]} is not the approved {str(b.get("specSha256"))[:16]}')
    if b.get('inventorySha256') != spec['derivedFrom']['sha256']:
        out.append('FAIL binding: spec not derived from the approved research inventory')
    if b.get('artifactSha256') != canonical_hashes or canonical_hashes != spec['artifactSha256']:
        out.append('FAIL binding: canonical artifacts differ from the approved artifacts')
    return out


# ------------------------------------------------------------------------------------------- chain reads
def safe_state(chain, safe):
    return sg.safe_state(chain, safe)


def identity_problems(chain, ctx, record, approved_manifests):
    """BLOCKED reasons where the approved identities disagree with the Fork state. Empty means verified."""
    a, act, out = ctx['addresses'], record['actors'], []
    for role in ROLE_KEYS:
        if not isinstance(act.get(role), str) or len(act[role]) != 42:
            out.append(f'BLOCKED identity: approved {role} missing')
    if out:
        return out
    tl = a['timelock']
    has = lambda target, role, who: chain.boolean(target, 'hasRole(bytes32,address)', role, who)
    if not (has(tl, TL_ROLES['PROPOSER'], act['gov']) and has(tl, TL_ROLES['EXECUTOR'], act['gov'])):
        out.append('BLOCKED identity: approved GOV is not the Timelock proposer and executor on the Fork')
    if not has(tl, TL_ROLES['CANCELLER'], act['can']):
        out.append('BLOCKED identity: approved CAN is not a Timelock canceller on the Fork')
    for key in ('stpros', 'oracle', 'bridge', 'distributor', 'factory'):
        if chain.addr(a[key], 'guardian()') != _lower(act['guardian']):
            out.append(f'BLOCKED identity: {key}.guardian() is not the approved GRD')
    if chain.addr(a['oracle'], 'slp()') != _lower(act['reporter']):
        out.append('BLOCKED identity: Oracle.slp() is not the approved reporter')
    # the commission recipient follows the owner's TD-9 decision: K2 = the Treasury Safe; K1 = the approved account
    # (E1 kept) plus Agent A's written NOT-APPLICABLE ruling for the Treasury Safe gate. No decision: BLOCKED.
    oi = record.get('ownerInputs') or {}
    commission = oi.get('commission') or {}
    account_now = chain.addr(a['oracle'], 'commissionAccount()')
    if commission.get('decision') == 'K2':
        if account_now != _lower(act['treasury']) or _lower(commission.get('account')) != _lower(act['treasury']):
            out.append('BLOCKED identity: K2 but Oracle.commissionAccount() is not the approved Treasury Safe')
    elif commission.get('decision') == 'K1':
        if account_now != _lower(commission.get('account')):
            out.append('BLOCKED identity: K1 but Oracle.commissionAccount() is not the approved (kept) account')
        if not (isinstance(commission.get('treasuryGateException'), str) and commission['treasuryGateException'].strip()):
            out.append('BLOCKED identity: K1 without the written Treasury Safe gate ruling (K1b)')
    else:
        out.append('BLOCKED identity: commission decision (TD-9 K1 or K2) not in the approved record')
    approved_dests = [_lower(d) for d in oi.get('destinations') or []]
    if not isinstance(oi.get('destinations'), list):
        out.append('BLOCKED identity: no approved ordered SLP destinations in the record')
    else:
        raw = chain.call(a['distributor'], 'getSlps()')
        if [x.lower() for x in abi.decode_parameters([{'type': 'address[]'}], raw)[0]] != approved_dests:
            out.append('BLOCKED identity: SlpDistributor.getSlps() differs from the approved ordered destinations')
    if chain.addr(a['distributor'], 'keeper()') != _lower(act['keeper']):
        out.append('BLOCKED identity: SlpDistributor.keeper() is not the approved keeper')
    for v in a['governedVaults']:
        if not has(v, OPERATOR, act['ops']):
            out.append(f'BLOCKED identity: approved OPS lacks OPERATOR_ROLE on governed vault {v[:10]}')
    if has(tl, TL_ROLES['PROPOSER'], act['can']) or has(tl, TL_ROLES['EXECUTOR'], act['can']):
        out.append('BLOCKED identity: approved CAN is also a Timelock proposer or executor')
    for key in ('e1', 'e2'):
        who = ctx['actors'].get(key)
        if who and any(has(tl, TL_ROLES[r], who) for r in ('PROPOSER', 'EXECUTOR', 'CANCELLER')):
            out.append(f'BLOCKED identity: {key} still holds a Timelock role')
    safes = {}
    for role in SAFE_ROLES:
        s = record['safes'].get(role)
        if s is None:
            out.append(f'BLOCKED identity: no approved Safe for {role}')
            continue
        if _lower(s.get('address')) != _lower(act[role]):
            out.append(f'BLOCKED identity: approved {role} Safe address differs from the approved actor')
            continue
        safes[role] = s
        out += [f'BLOCKED identity: {p}' for p in sg.safe_problems(chain, role, s)]
        if s.get('safeManifestSha256') not in approved_manifests:
            out.append(f'BLOCKED identity: {role} Safe identity manifest is not approved (FR-X8)')
    out += [f'BLOCKED identity: {p}' for p in sg.policy_problems(safes, deployer=act.get('deployer'), keeper=act['keeper'],
                                                                   reporter=act['reporter'])]
    return out


def postcheck(run):
    """The upgrade transaction executed and the required POST / GC-03 checks exist and pass. Zero checks is MISSING,
    never PASS."""
    route = getattr(run, 'gov_route', None) or {}
    mode = route.get('mode', 'SAFE' if run.m['chainId'] == 1672 else 'EOA')
    rule = POSTCHECK.get((run.m['chainId'], mode)) or POSTCHECK.get(run.m['chainId'])
    if rule is None:
        return {'verdict': 'MISSING', 'why': ['no postcheck rule for this chain']}
    why = []
    txs = [t for t in run.chain.transcript if t['label'] == rule['tx']]
    if not txs:
        why.append(f'upgrade transaction {rule["tx"]} not in the transcript')
    elif not all(t['status'] == 1 and t['expectSuccess'] for t in txs[-1:]):
        why.append(f'upgrade transaction {rule["tx"]} did not succeed')
    checks = {c['id']: c['verdict'] for c in run.checks}
    for cid in rule['checks']:
        if checks.get(cid) != 'PASS':
            why.append(f'required check {cid} is {checks.get(cid, "absent")}')
    post = [c for c in run.checks if c['id'].startswith('POST-')]
    if len(post) < rule['postMin']:
        why.append(f'{len(post)} POST checks, at least {rule["postMin"]} required')
    failed = [c['id'] for c in post if c['verdict'] != 'PASS']
    if failed:
        why.append(f'POST checks not PASS: {failed[:5]}')
    s_post = getattr(run, 's_post', None)
    linked = s_post is not None and run.chain.block()['hash'] == s_post['hash']
    if s_post is not None and not linked:
        why.append('the node moved after the upgrade postchecks: the E2E would not start from S_post')
    verdict = 'PASS' if not why else ('MISSING' if not txs or len(post) < rule['postMin'] else 'FAIL')
    upgrade_time = int(run.chain.block(hex(txs[-1]['block']))['timestamp'], 16) if txs and 'block' in txs[-1] else None
    return {'verdict': verdict, 'why': why, 'postChecks': len(post), 'upgradeTx': rule['tx'],
            'upgradeTxHash': txs[-1]['transactionHash'] if txs else None,
            'upgradeTxFrom': txs[-1]['from'] if txs else None, 'upgradeTime': upgrade_time, 'route': mode,
            'routeClass': route.get('class'), 'sPost': s_post, 'sPostLinked': linked if s_post else None}


def history_authenticated(run, record, provenance_implemented):
    """(authenticated, reasons). True only if the run topology, the final provenance gate and the approved record
    all agree; FH-D2 is not lowered."""
    reasons = []
    prov = (run.topo.get('binding') or {}).get('provenance') or {}
    if prov.get('authenticated') is not True:
        reasons.append('run topology history provenance: authenticated = false (FH-D2)')
    if not provenance_implemented:
        reasons.append('final provenance gate not implemented (FINAL_GATE_PROVENANCE_IMPLEMENTED = False)')
    if record is None:
        reasons.append('no approved release record')
    else:
        h = record.get('history', {})
        if h.get('s1ManifestSha256') != (run.topo.get('binding') or {}).get('s1ManifestSha256'):
            reasons.append('approved record binds another S1 history manifest')
    return (not reasons), reasons


def read_authorization(run, record):
    binding = run.topo.get('binding') or {}
    if not run.real or not binding.get('forkBlockHash'):
        return False
    return bool(record) and record.get('readAuthorization', {}).get('specSha256') == binding.get('specSha256')
