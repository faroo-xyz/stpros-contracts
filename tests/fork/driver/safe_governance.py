"""Safe governance route for the Fork driver and the E2E harness: approved Safe identities, the release-mode signer
policy, the local Safe model, and a checked approved-hash execution. Test-only.

``checked_execute`` wraps ``safe_exec.execute`` (the route of ``SafeExecutionAdapter``) and makes every property the
release route depends on an explicit check row:
- before anything is sent:
  - the node's Safe owners and threshold equal the approved ones;
  - the nonce equals the expected one when a sequence is pinned;
  - the signers are approved owners, exactly ``threshold`` of them, strictly increasing (decoded independently by
    ``safe_route.approved_hash_signers``).

  A failure here sends nothing.
- after execution:
  - the Safe's own hash equals the independent EIP-712 hash;
  - each approval is recorded on the Safe;
  - the nonce advanced by exactly one (or not at all for a required rejection);
  - the Safe emitted exactly one ExecutionSuccess for that hash;
  - the outer receipt status agrees with the inner execution;
  - an optional inner-effect check holds (for example every Timelock batch item executed).

So an inner failure can never hide behind a successful outer transaction (a ``safeTxGas != 0`` Safe transaction
with ExecutionFailure is a FAIL).

``deploy_model`` installs the local Safe model (``tests/fork/synthetic/SyntheticSafe.sol``) from this export's forge
output. Its identity is derived from the artifacts, not read back from the node. It is SYNTHETIC: it proves the
route, never a deployed Safe (FR-X8).
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from localchain import ExecutionError  # noqa: E402
from models import truth  # noqa: E402
import safe_exec  # noqa: E402
import safe_route as sr  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

ZERO = '0x' + '00' * 20
SENTINEL = '0x' + '00' * 19 + '01'
SAFE_GUARD_SLOT = '0x4a204f620c8c5ccdca3fd54d003badd85ba500436a431f0cbda4f558c93c34c8'
SAFE_OUT = ROOT / 'out' / 'SyntheticSafe.sol'
# Release-mode profile (tools/release/native/inputs.py; RELEASE-DECISIONS TD-8): owners, threshold.
PROFILE = {'gov': (5, 3), 'can': (5, 3), 'guardian': (4, 2), 'ops': (4, 2), 'treasury': (3, 2)}


def _lower(a):
    return a.lower() if isinstance(a, str) else a


def code_hash(chain, target):
    code = chain.code(target)
    return None if code in ('0x', '0x0', None) else keccak256(bytes.fromhex(code[2:]))


# --------------------------------------------------------------------------------------------- identity
def safe_state(chain, safe):
    """What the node says about a Safe: code, runtime and singleton hashes, owners, threshold, nonce, modules, guard."""
    runtime = code_hash(chain, safe)
    if runtime is None:
        return {'code': False}
    owners = [a.lower() for a in abi.decode_parameters([{'type': 'address[]'}], chain.call(safe, 'getOwners()'))[0]]
    try:
        raw = chain.call(safe, 'getModulesPaginated(address,uint256)', SENTINEL, 10)
        modules = [a.lower() for a in abi.decode_parameters([{'type': 'address[]'}, {'type': 'address'}], raw)[0]]
    except ValueError:
        modules = None
    singleton = '0x' + chain.slot(safe, '0x0')[-40:].lower()
    return {'code': True, 'owners': owners, 'threshold': chain.uint(safe, 'getThreshold()'), 'nonce': chain.uint(safe, 'nonce()'),
            'modules': modules, 'guard': '0x' + chain.slot(safe, SAFE_GUARD_SLOT)[-40:].lower(), 'singleton': singleton,
            'runtimeHash': runtime, 'codeHash': runtime, 'singletonHash': code_hash(chain, singleton)}


def safe_problems(chain, role, expected, *, check_nonce=False):
    """Differences between an approved Safe identity (release-tool ``review.safes`` fields) and the node."""
    addr = _lower(expected.get('address'))
    st = safe_state(chain, addr) if addr else {'code': False}
    if not st['code']:
        return [f'{role} Safe {addr} has no code']
    out = []
    for key in ('runtimeHash', 'singleton', 'singletonHash'):
        if _lower(expected.get(key)) != _lower(st[key]):
            out.append(f'{role} Safe {key} {str(st[key])[:18]} differs from the approved {str(expected.get(key))[:18]}')
    if sorted(st['owners']) != sorted(_lower(o) for o in expected.get('owners', [])):
        out.append(f'{role} Safe owners on the node differ from the approved owners')
    if st['threshold'] != expected.get('threshold'):
        out.append(f'{role} Safe threshold {st["threshold"]} is not the approved {expected.get("threshold")}')
    if st['modules'] != [] or st['guard'] != ZERO:
        out.append(f'{role} Safe has modules or a guard')
    if check_nonce and 'nonce' in expected and st['nonce'] != expected['nonce']:
        out.append(f'{role} Safe nonce {st["nonce"]} is not the expected {expected["nonce"]}')
    return out


def policy_problems(safes, *, deployer=None, keeper=None, reporter=None):
    """The release-mode signer rules (``tools/release/native/inputs.py``) on the Safes given (any subset of roles)."""
    out, owners = [], {r: {_lower(o) for o in s.get('owners', [])} for r, s in safes.items()}
    if 'gov' in safes and (safes['gov'].get('threshold') != 3 or len(owners['gov']) != 5):
        out.append('GOV_THRESHOLD: GOV Safe must be exactly 3 of 5')
    if 'can' in safes and not (safes['can'].get('threshold') or 0) >= 3:
        out.append('CAN_THRESHOLD: CAN Safe threshold must be >= 3')
    if 'gov' in safes and 'can' in safes and _lower(safes['gov'].get('address')) == _lower(safes['can'].get('address')):
        out.append('GOV and CAN are the same Safe')
    for left, right in (('gov', 'can'), ('gov', 'guardian'), ('can', 'guardian'), ('ops', 'gov'), ('ops', 'can')):
        if left in owners and right in owners and owners[left] & owners[right]:
            out.append(f'SAFE_SIGNER_OVERLAP: {left} and {right} share signers')
    for role, signers in owners.items():
        if deployer and _lower(deployer) in signers:
            out.append(f'DEPLOYER_SIGNER: the deployer signs the {role} Safe')
    for role in ('gov', 'can'):
        for who, name in ((keeper, 'keeper'), (reporter, 'reporter')):
            if role in owners and who and _lower(who) in owners[role]:
                out.append(f'OPERATIONAL_SAFE_SIGNER: the {name} signs the {role} Safe')
    return out


# --------------------------------------------------------------------------------------------- local model
def _artifact(name):
    path = SAFE_OUT / f'{name}.json'
    if not path.is_file():
        raise ExecutionError(f'forge output {path} missing: build the export first')
    return json.loads(path.read_text())


def model_owners(network, role, count):
    return sorted(('0x' + keccak256(f'faroo safe model {network} {role} owner {i}'.encode())[-40:] for i in range(count)),
                  key=lambda a: int(a, 16))


def deploy_model(chain, network, roles, *, deployer=None, profile=PROFILE):
    """SYNTHETIC: one local Safe model per role. Returns {role: approved-identity dict} with hashes taken from the
    forge artifacts (the node is then checked against them)."""
    deployer = deployer or '0x' + keccak256(f'faroo safe model {network} deployer'.encode())[-40:]
    single = _artifact('SyntheticSafeSingleton')
    proxy = _artifact('SyntheticSafeProxy')
    singleton, _ = chain.create('SAFE.model.singleton', deployer, single['bytecode']['object'])
    out = {}
    for role in roles:
        count, threshold = profile[role]
        owners = model_owners(network, role, count)
        safe, _ = chain.create(f'SAFE.model.{role}', deployer, proxy['bytecode']['object'] + abi.enc_word(singleton))
        setup = abi.encode_function({'type': 'function', 'name': 'setup', 'inputs': [{'type': 'address[]'}, {'type': 'uint256'}]},
                                    [owners, threshold])
        _, rec = chain.send(f'SAFE.model.{role}.setup', owners[0], safe, setup)
        if rec['status'] != 1:
            raise ExecutionError(f'Safe model setup for {role} failed')
        out[role] = {'address': safe.lower(), 'owners': owners, 'threshold': threshold, 'nonce': 0, 'modules': [], 'guard': ZERO,
                     'runtimeHash': keccak256(bytes.fromhex(proxy['deployedBytecode']['object'][2:])),
                     'singleton': singleton.lower(),
                     'singletonHash': keccak256(bytes.fromhex(single['deployedBytecode']['object'][2:])),
                     'source': 'SYNTHETIC_MODEL'}
    return out


# --------------------------------------------------------------------------------------------- execution
def checked_execute(chain, label, spec, to, data, *, expect=True, expected_nonce=None, signers=None, safe_tx_gas=0,
                    inner_check=None):
    """One Safe transaction by the approved route, with its checks. Returns (receipt or None, rows, facts)."""
    safe = _lower(spec['address'])
    st = safe_state(chain, safe)
    rows = [truth(f'{label}.safe.code', st['code'], 'no code at the Safe address')]
    if not st['code']:
        return None, rows, {}
    rows += [truth(f'{label}.safe.owners==approved', sorted(st['owners']) == sorted(_lower(o) for o in spec['owners']),
                   f'node owners {st["owners"]}'),
             truth(f'{label}.safe.threshold==approved', st['threshold'] == spec['threshold'],
                   f'node threshold {st["threshold"]}, approved {spec["threshold"]}')]
    if expected_nonce is not None:
        rows.append(truth(f'{label}.safe.nonce==expected', st['nonce'] == expected_nonce, f'node nonce {st["nonce"]}, expected {expected_nonce}'))
    chosen = sorted((_lower(s) for s in (signers or spec['owners'][:spec['threshold']])), key=lambda a: int(a, 16))
    try:
        decoded = sr.approved_hash_signers(safe_exec.signatures(chosen), st['threshold'])
        signer_ok = all(s in st['owners'] for s in decoded) and len(set(decoded)) == len(decoded)
        why = '' if signer_ok else f'signers {decoded} are not all Safe owners'
    except sr.ReplayValidationError as error:
        signer_ok, why = False, str(error)
    rows.append(truth(f'{label}.signers.validOrderedThreshold', signer_ok, why))
    if not all(r['verdict'] == 'PASS' for r in rows):
        rows.append({'id': f'{label}.notExecuted', 'verdict': 'FAIL', 'expected': '1', 'actual': '0',
                     'detail': 'Safe identity, nonce or signer check failed: nothing was sent'})
        return None, rows, {}
    tx = safe_exec.safe_tx(to, data, 0)
    tx['safeTxGas'] = safe_tx_gas
    try:
        records, checks, receipt, tx_hash = safe_exec.execute(chain, label, safe, list(spec['owners']), st['threshold'], tx,
                                                              expect=expect, signers=chosen)
    except ExecutionError as error:
        rows.append({'id': f'{label}.safeTxHash==independentEIP712', 'verdict': 'FAIL', 'expected': '1', 'actual': '0',
                     'detail': str(error)[:300]})
        return None, rows, {}
    rows += [truth(c['id'], c['ok']) for c in checks]
    outer = int(receipt['status'], 16) == 1
    events = sr.execution_events(receipt, safe)
    inner = events == [('SUCCESS', tx_hash.lower(), 0)]
    after = chain.uint(safe, 'nonce()')
    approvals = [chain.uint(safe, 'approvedHashes(address,bytes32)', s, tx_hash) == 1 for s in chosen[:-1]]
    rows += [truth(f'{label}.approvalsRecorded', all(approvals), f'approvals {approvals}'),
             truth(f'{label}.outer==inner', outer == inner, f'outer status {int(outer)}, Safe events {events}'),
             truth(f'{label}.nonce+{int(outer)}', after == st['nonce'] + int(outer), f'{st["nonce"]} -> {after}')]
    if expect:
        rows.append(truth(f'{label}.executionSuccess', inner, f'Safe events {events}'))
        if inner_check is not None:
            ok, detail = inner_check(receipt)
            rows.append(truth(f'{label}.innerEffect', ok, detail))
    facts = {'safe': safe, 'safeTxHash': tx_hash, 'signers': chosen, 'executor': chosen[-1], 'nonceBefore': st['nonce'],
             'nonceAfter': after, 'outerStatus': int(outer), 'safeEvents': [e[0] for e in events],
             'outerReceiptGas': records[-1]['gasUsed'], 'approvalGas': [r['gasUsed'] for r in records[:-1]],
             'transactionHash': receipt['transactionHash']}
    return receipt, rows, facts
