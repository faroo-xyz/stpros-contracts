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
# The upgrade transaction and the checks that must exist after it, per chain.
POSTCHECK = {
    688689: {'tx': 'U_t.executeBatch', 'checks': ('tx.U_t.executeBatch', 'GC-03.afterU_t.doneNoPending'), 'postMin': 20},
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
    code = chain.code(safe)
    if code in ('0x', '0x0', None):
        return {'code': False}
    owners = [a.lower() for a in abi.decode_parameters([{'type': 'address[]'}], chain.call(safe, 'getOwners()'))[0]]
    try:
        raw = chain.call(safe, 'getModulesPaginated(address,uint256)', SENTINEL, 10)
        modules = [a.lower() for a in abi.decode_parameters([{'type': 'address[]'}, {'type': 'address'}], raw)[0]]
    except ValueError:
        modules = None
    return {'code': True, 'owners': owners, 'threshold': chain.uint(safe, 'getThreshold()'), 'modules': modules,
            'guard': '0x' + chain.slot(safe, SAFE_GUARD_SLOT)[-40:].lower(), 'singleton': '0x' + chain.slot(safe, '0x0')[-40:].lower(),
            'codeHash': keccak256(bytes.fromhex(code[2:]))}


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
    if chain.addr(a['oracle'], 'commissionAccount()') != _lower(act['treasury']):
        out.append('BLOCKED identity: Oracle.commissionAccount() is not the approved treasury')
    if chain.addr(a['distributor'], 'keeper()') != _lower(act['keeper']):
        out.append('BLOCKED identity: SlpDistributor.keeper() is not the approved keeper')
    for v in a['governedVaults']:
        if not has(v, OPERATOR, act['ops']):
            out.append(f'BLOCKED identity: approved OPS lacks OPERATOR_ROLE on governed vault {v[:10]}')
    for role in SAFE_ROLES:
        s = record['safes'].get(role)
        if s is None:
            out.append(f'BLOCKED identity: no approved Safe for {role}')
            continue
        if _lower(s.get('address')) != _lower(act[role]):
            out.append(f'BLOCKED identity: approved {role} Safe address differs from the approved actor')
            continue
        st = safe_state(chain, s['address'])
        if not st['code']:
            out.append(f'BLOCKED identity: {role} Safe has no code on the Fork')
            continue
        if sorted(st['owners']) != sorted(_lower(o) for o in s.get('owners', [])):
            out.append(f'BLOCKED identity: {role} Safe owners on the Fork differ from the approved owners')
        if st['threshold'] != s.get('threshold'):
            out.append(f'BLOCKED identity: {role} Safe threshold {st["threshold"]} is not the approved {s.get("threshold")}')
        if st['modules'] != [] or st['guard'] != '0x' + '00' * 20:
            out.append(f'BLOCKED identity: {role} Safe has modules or a guard')
        if s.get('safeManifestSha256') not in approved_manifests:
            out.append(f'BLOCKED identity: {role} Safe identity manifest is not approved (FR-X8)')
    return out


def postcheck(run):
    """The upgrade transaction executed and the required POST / GC-03 checks exist and pass. Zero checks is MISSING,
    never PASS."""
    rule = POSTCHECK.get(run.m['chainId'])
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
    verdict = 'PASS' if not why else ('MISSING' if not txs or len(post) < rule['postMin'] else 'FAIL')
    upgrade_time = int(run.chain.block(hex(txs[-1]['block']))['timestamp'], 16) if txs and 'block' in txs[-1] else None
    return {'verdict': verdict, 'why': why, 'postChecks': len(post), 'upgradeTx': rule['tx'],
            'upgradeTxHash': txs[-1]['transactionHash'] if txs else None,
            'upgradeTxFrom': txs[-1]['from'] if txs else None, 'upgradeTime': upgrade_time}


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
