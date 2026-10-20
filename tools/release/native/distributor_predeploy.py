"""Mainnet SlpDistributor pre-deployment: unsigned definitions, read-only verification and release binding.

Purpose: deploy the production SlpDistributor proxy address early, so the SLP team can configure its services. The
later V4 release (P-0 / H / U) **reuses** this proxy, its implementation and its Timelock: the release config
records them in ``review.predeployed`` and ``inputs.validate_predeployed`` / ``checkpoint.verify_predeployed``
admit them only when they are bound to this plan and to the chain.

What the definitions do, from the deployer EOA, in this order:
1. Deploy the SlpDistributor V4 implementation: canonical creation bytes; ``initialize`` is constructor-only.
2. Deploy the TimelockController with a 600 s delay (configuration phase), GOV Safe as proposer and executor, and
   the deployer as temporary admin.
3. The Timelock grants CANCELLER to the CAN Safe.
4. The deployer renounces the Timelock admin.
5. Deploy the OZ v4 TransparentUpgradeableProxy: the recorded creation code, the **shared mainnet ProxyAdmin** as
   admin, and ``initialize(owner = Timelock, keeper, destinations, guardian)`` in its constructor.

Identity binding: the ProxyAdmin and the GOV Safe are the recorded mainnet identities of
``tools/release/registry.json``. Any other ProxyAdmin or GOV Safe is refused. The verification reads the
ProxyAdmin's runtime and owner from the chain and compares them with the recorded values, not with the inputs. A
Safe without reviewed identity evidence (manifest, threshold, signer independence) is a deployment blocker.

Initial safety state, recommended and checked:
- keeper = 0 and an empty whitelist, so ``distribute`` reverts for everyone (``NotKeeper``) and no SLP can be paid;
- ``receive()`` stays open (contract behaviour, unchanged): anyone can send native PROS to the proxy. The
  pre-deployment does **not** make it a production sink. No protocol flow sends funds there until U migrates
  StPROS to it, and the GOV Safe configures the keeper and the whitelist through 600 s Timelock operations;
- the keeper and the whitelist are set later by 600 s Timelock operations of the GOV Safe.

Addresses are CREATE-derived from the deployer and its nonce and are reported as **PREDICTED**. They are never a
deployment record. Only the five transactions on chain, bound by ``checkpoint.verify_predeployed``, prove a
deployment, and only the read-only verification accepts it. Nothing here signs or sends.
"""
import hashlib
import json
from pathlib import Path

from ...lib import abi
from ...lib.keccak import keccak256
from ._shared import bigint, require

MAINNET = 1672
CONFIG_DELAY = 600
ZERO = '0x' + '00' * 20
ROLE = {'admin': '0x' + '00' * 32, 'proposer': keccak256(b'PROPOSER_ROLE'), 'executor': keccak256(b'EXECUTOR_ROLE'),
        'canceller': keccak256(b'CANCELLER_ROLE')}
EIP1967_ADMIN = '0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103'
EIP1967_IMPL = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
# Recorded mainnet runtimes (tests/fixtures/legacy/mainnet, checked on chain at block 19,774,476, status MATCH):
# the shared OZ v4 ProxyAdmin at 0x4238..., and the OZ v4 TransparentUpgradeableProxy runtime of every mainnet proxy.
PROXY_ADMIN_RUNTIME = '0xfdb374f62c5df71a60c5965707d6653c4910b1e309693a88263492ade756df11'
TUP_V4_RUNTIME = '0xe5e3693157141608a301682c8c228c0277eac7efc0b98b57f874ca49752b5fd8'
# Creation code (without constructor arguments) of that recorded OZ v4 proxy: the fixture's creationCode, compiled from
# the recorded source (solc 0.8.10), keccak256 0x2263a775... (the CREATE prefix of step 5).
TUP_V4_CREATION_KECCAK = '0x2263a7750608b79bba01684bd6cac686d9d06799f954229e6d7abb1dc4667890'
# The reviewed creation bytecode of the canonical contracts is pinned by the protected artifact reference
# (``tools/ci/r4-reference.json``: fieldSha256.creationBytecode = SHA-256 of the canonical JSON of the hex string).
REFERENCE = Path(__file__).resolve().parents[2] / 'ci' / 'r4-reference.json'
STEP_KINDS = {1: 'CREATE', 2: 'CREATE', 3: 'CALL', 4: 'CALL', 5: 'CREATE'}


def reviewed_creation(name, code):
    """True only if ``code`` is exactly the reviewed creation bytecode of ``name`` (no constructor arguments):
    SlpDistributor and TimelockController by the protected reference, the OZ v4 proxy by its recorded keccak."""
    if not (isinstance(code, str) and code.startswith('0x') and len(code) > 2 and len(code) % 2 == 0):
        return False
    if name == 'tupV4':
        return keccak256(bytes.fromhex(code[2:])) == TUP_V4_CREATION_KECCAK
    pinned = json.loads(REFERENCE.read_text())['contracts'][name]['fieldSha256']['creationBytecode']
    return hashlib.sha256((json.dumps(code, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode()).hexdigest() == pinned
SAFE_ROLES = ('gov', 'can', 'guardian')


def recorded_mainnet():
    """The recorded identities the pre-deployment binds to (``tools/release/registry.json``)."""
    registry = json.loads((Path(__file__).resolve().parents[1] / 'registry.json').read_text())['mainnet']
    gov = [a for a, label in registry['accounts'].items() if 'owner of ProxyAdmin' in label]
    require(len(gov) == 1, 'PREDEPLOY_REGISTRY', 'exactly one recorded GOV Safe')
    return {'source': 'tools/release/registry.json (mainnet)', 'proxyAdmin': registry['proxy_admin'], 'govSafe': gov[0],
            'proxyAdminRuntimeKeccak': PROXY_ADMIN_RUNTIME, 'proxyRuntimeKeccak': TUP_V4_RUNTIME}


def _addr(value, what, zero_ok=False):
    ok = isinstance(value, str) and len(value) == 42 and value.startswith('0x')
    require(ok and (zero_ok or int(value, 16) != 0), 'PREDEPLOY_INPUT', what)
    return value.lower()


def _rlp_int(n):
    if n == 0:
        return b'\x80'
    raw = n.to_bytes((n.bit_length() + 7) // 8, 'big')
    return raw if len(raw) == 1 and raw[0] < 0x80 else bytes([0x80 + len(raw)]) + raw


def create_address(sender, nonce):
    """CREATE address: keccak256(rlp([sender, nonce]))[12:]."""
    body = bytes([0x94]) + bytes.fromhex(sender[2:]) + _rlp_int(nonce)
    return '0x' + keccak256(bytes([0xc0 + len(body)]) + body)[-40:]


def _fn(name, *types):
    return {'type': 'function', 'name': name, 'inputs': [{'type': t} for t in types]}


def _args(types, values):
    return abi.encode_parameters([{'type': t} for t in types], values)[2:]


def timelock_args(gov, deployer):
    return _args(('uint256', 'address[]', 'address[]', 'address'), [CONFIG_DELAY, [gov], [gov], deployer])


def proxy_args(impl, proxy_admin, timelock, keeper, destinations, guardian):
    init = abi.encode_function(_fn('initialize', 'address', 'address', 'address[]', 'address'), [timelock, keeper, destinations, guardian])
    return _args(('address', 'address', 'bytes'), [impl, proxy_admin, init])


def _safe_evidence(inputs):
    """Reviewed Safe identity evidence by reference ({manifestSha256, approvalReference}); a role without it is a
    deployment blocker. This module never reads signers, keys or wallets."""
    evidence, blockers = {}, []
    given = inputs.get('safeEvidence') or {}
    for role in SAFE_ROLES:
        e = given.get(role)
        ok = (isinstance(e, dict) and isinstance(e.get('manifestSha256'), str) and len(e['manifestSha256']) == 64
              and isinstance(e.get('approvalReference'), str) and e['approvalReference'])
        if ok:
            evidence[role] = {'manifestSha256': e['manifestSha256'].lower(), 'approvalReference': e['approvalReference']}
        else:
            blockers.append(f'{role.upper()}_SAFE_IDENTITY_UNREVIEWED: no reviewed Safe manifest (identity, threshold, '
                            'signer independence)')
    return evidence, blockers


def plan(inputs, artifacts, recorded=None):
    """``inputs``: chainId, deployer, deployerNonce, govSafe, canSafe, guardian, keeper, destinations, proxyAdmin,
    optional safeEvidence. ``artifacts``: SlpDistributor and TimelockController canonical artifacts; tupV4
    {creationCode, runtimeKeccak}. ``recorded`` defaults to the registry's mainnet identities; a test that passes its
    own topology gets a plan labelled SYNTHETIC."""
    synthetic = recorded is not None
    recorded = recorded or recorded_mainnet()
    require(inputs.get('chainId') == MAINNET, 'PREDEPLOY_CHAIN', str(inputs.get('chainId')))
    dep, gov, can = _addr(inputs['deployer'], 'deployer'), _addr(inputs['govSafe'], 'govSafe'), _addr(inputs['canSafe'], 'canSafe')
    grd, pa = _addr(inputs['guardian'], 'guardian'), _addr(inputs['proxyAdmin'], 'proxyAdmin')
    require(pa == recorded['proxyAdmin'], 'PREDEPLOY_PROXY_ADMIN', f'{pa} is not the recorded shared ProxyAdmin {recorded["proxyAdmin"]}')
    require(gov == recorded['govSafe'], 'PREDEPLOY_GOV_SAFE', f'{gov} is not the recorded GOV Safe {recorded["govSafe"]}')
    keeper = _addr(inputs.get('keeper', ZERO), 'keeper', zero_ok=True)
    dests = [_addr(d, 'destination') for d in inputs.get('destinations', [])]
    nonce = inputs['deployerNonce']
    require(isinstance(nonce, int) and nonce >= 0, 'PREDEPLOY_INPUT', 'deployerNonce')
    require(len({dep, gov, can, grd, pa}) == 5 and keeper not in (gov, can, pa), 'PREDEPLOY_ACTOR_SEPARATION')
    for name in ('SlpDistributor', 'TimelockController'):
        require(not artifacts[name].get('creationLinks') and not artifacts[name].get('immutableReferences'),
                'PREDEPLOY_ARTIFACT', f'{name} links or immutables need review')
    require(artifacts['tupV4']['runtimeKeccak'] == recorded['proxyRuntimeKeccak'], 'PREDEPLOY_ARTIFACT', 'proxy is not the recorded OZ v4 TUP')
    for name, code in (('SlpDistributor', artifacts['SlpDistributor']['creationBytecode']),
                       ('TimelockController', artifacts['TimelockController']['creationBytecode']),
                       ('tupV4', artifacts['tupV4']['creationCode'])):
        require(reviewed_creation(name, code), 'PREDEPLOY_ARTIFACT', f'{name} creation code is not the reviewed one')
    evidence, blockers = _safe_evidence(inputs)
    impl, tl = create_address(dep, nonce), create_address(dep, nonce + 1)
    proxy = create_address(dep, nonce + 4)
    txs = [
        {'step': 1, 'kind': 'CREATE', 'from': dep, 'nonce': nonce, 'contract': 'SlpDistributor (V4 implementation)',
         'data': artifacts['SlpDistributor']['creationBytecode'], 'predictedAddress': impl},
        {'step': 2, 'kind': 'CREATE', 'from': dep, 'nonce': nonce + 1, 'contract': f'TimelockController({CONFIG_DELAY} s)',
         'data': artifacts['TimelockController']['creationBytecode'] + timelock_args(gov, dep), 'predictedAddress': tl},
        {'step': 3, 'kind': 'CALL', 'from': dep, 'nonce': nonce + 2, 'to': tl, 'function': 'grantRole(CANCELLER, CAN)',
         'data': abi.encode_function(_fn('grantRole', 'bytes32', 'address'), [ROLE['canceller'], can])},
        {'step': 4, 'kind': 'CALL', 'from': dep, 'nonce': nonce + 3, 'to': tl, 'function': 'renounceRole(ADMIN, deployer)',
         'data': abi.encode_function(_fn('renounceRole', 'bytes32', 'address'), [ROLE['admin'], dep])},
        {'step': 5, 'kind': 'CREATE', 'from': dep, 'nonce': nonce + 4, 'contract': 'TransparentUpgradeableProxy (OZ v4)',
         'data': artifacts['tupV4']['creationCode'] + proxy_args(impl, pa, tl, keeper, dests, grd), 'predictedAddress': proxy},
    ]
    status = 'UNSIGNED DEPLOYMENT DEFINITIONS: NOT DEPLOYED; every address is PREDICTED until its transaction is verified on chain'
    return {
        'schema': 'faroo-slp-distributor-predeploy-plan-v2',
        'status': ('SYNTHETIC TOPOLOGY — ' if synthetic else '') + status,
        'deployable': False, 'chainId': MAINNET,
        'recordedIdentities': {**recorded, 'class': 'SYNTHETIC (test topology)' if synthetic else 'RECORDED MAINNET'},
        'deploymentBlockers': blockers, 'safeEvidence': evidence,
        'predicted': {'implementation': impl, 'timelock': tl, 'proxy': proxy},
        'inputs': {'deployer': dep, 'deployerNonce': nonce, 'govSafe': gov, 'canSafe': can, 'guardian': grd, 'keeper': keeper,
                   'destinations': dests, 'proxyAdmin': pa},
        'transactions': txs,
        'expected': {'implementationRuntimeKeccak': artifacts['SlpDistributor']['runtimeKeccak'],
                     'timelockRuntimeKeccak': artifacts['TimelockController']['runtimeKeccak'],
                     'proxyRuntimeKeccak': recorded['proxyRuntimeKeccak'],
                     'proxyAdminRuntimeKeccak': recorded['proxyAdminRuntimeKeccak'], 'proxyAdminOwner': recorded['govSafe'],
                     'minDelay': CONFIG_DELAY, 'initializedVersion': 4},
        'funds': 'receive() is open: anyone can send native PROS to the proxy. The pre-deployment is not a production '
                 'sink: no protocol flow sends funds there before U, and any earlier balance is outside protocol accounting',
        'reuse': 'The V4 release records this plan and the five transaction hashes in review.predeployed; the release '
                 'then reuses this Timelock, implementation and proxy and emits no second deployment definition for them',
    }


def verify(observed, p):
    """Read-only acceptance of an executed pre-deployment. ``observed`` comes from chain reads, never from the plan.
    The ProxyAdmin runtime and owner are compared with the recorded identities. Returns the failed checks (empty =
    accepted); the plan's deployment blockers are reported separately and never cleared here."""
    out, e, i = [], p['expected'], p['inputs']
    a = observed.get('addresses', {})
    for key in ('implementation', 'timelock', 'proxy'):
        if a.get(key) != p['predicted'][key]:
            out.append(f'{key} address {a.get(key)} is not the planned {p["predicted"][key]} (wrong nonce or order)')
    checks = [
        ('implementationRuntime', observed.get('implementationRuntimeKeccak') == e['implementationRuntimeKeccak']),
        ('timelockRuntime', observed.get('timelockRuntimeKeccak') == e['timelockRuntimeKeccak']),
        ('proxyRuntime', observed.get('proxyRuntimeKeccak') == e['proxyRuntimeKeccak']),
        ('proxyAdminRuntime==recorded', observed.get('proxyAdminRuntimeKeccak') == e['proxyAdminRuntimeKeccak']),
        ('proxyAdminOwner==recorded GOV Safe', observed.get('proxyAdminOwner') == e['proxyAdminOwner']),
        ('proxyAdminSlot', observed.get('adminSlot') == p['recordedIdentities']['proxyAdmin']),
        ('implementationSlot', observed.get('implementationSlot') == p['predicted']['implementation']),
        ('initializedVersion', observed.get('initializedVersion') == e['initializedVersion']),
        ('owner==Timelock', observed.get('owner') == p['predicted']['timelock']),
        ('pendingOwner==0', observed.get('pendingOwner') == ZERO),
        ('guardian', observed.get('guardian') == i['guardian']),
        ('keeper', observed.get('keeper') == i['keeper']),
        ('destinations', observed.get('destinations') == i['destinations']),
        ('minDelay==600', observed.get('minDelay') == e['minDelay']),
        ('timelockRoles', observed.get('roles') == {'govProposer': True, 'govExecutor': True, 'canCanceller': True,
                                                    'deployerAdmin': False, 'deployerProposer': False, 'timelockSelfAdmin': True}),
        ('notPaused', observed.get('paused') is False),
    ]
    out += [name for name, ok in checks if not ok]
    if i['keeper'] == ZERO and observed.get('distributeByAnyoneReverts') is not True:
        out.append('distribution not disabled')
    return out


def binding_problems(p, config, artifacts):
    """Why a recorded pre-deployment plan does not bind to this release config (empty = bound). Pure: the chain side
    is ``checkpoint.verify_predeployed`` plus the release preflight's code, proxy, owner, guardian, delay and role
    checks on the same addresses."""
    out = []
    if not isinstance(p, dict) or p.get('schema') != 'faroo-slp-distributor-predeploy-plan-v2' or p.get('chainId') != MAINNET:
        return ['not a mainnet pre-deployment plan (schema v2)']
    addresses, actors, impls = config['addresses'], config['actors'], config['implementations']
    pred, inp, exp = p['predicted'], dict(p['inputs']), p['expected']
    nonce = inp['deployerNonce']
    if not (isinstance(nonce, (int, float)) and nonce >= 0 and int(nonce) == nonce):
        return ['deployerNonce is not a non-negative integer']
    inp['deployerNonce'] = int(nonce)
    for ok, why in (
            (pred.get('timelock') == addresses.get('timelock'), 'planned Timelock is not addresses.timelock'),
            (pred.get('proxy') == addresses.get('distributor'), 'planned proxy is not addresses.distributor'),
            (pred.get('implementation') == impls.get('distributor'), 'planned implementation is not implementations.distributor'),
            (inp.get('govSafe') == actors.get('gov'), 'planned GOV Safe is not actors.gov'),
            (inp.get('canSafe') == actors.get('can'), 'planned CAN Safe is not actors.can'),
            (inp.get('guardian') == actors.get('guardian'), 'planned Guardian is not actors.guardian'),
            (inp.get('proxyAdmin') == addresses.get('proxyAdmin'), 'planned ProxyAdmin is not addresses.proxyAdmin'),
            (pred.get('implementation') == create_address(inp['deployer'], inp['deployerNonce'])
             and pred.get('timelock') == create_address(inp['deployer'], inp['deployerNonce'] + 1)
             and pred.get('proxy') == create_address(inp['deployer'], inp['deployerNonce'] + 4),
             'predicted addresses are not the CREATE addresses of the deployer and its nonces'),
            (exp.get('minDelay') == CONFIG_DELAY and bigint(config['policy']['minDelay']) == CONFIG_DELAY,
             'planned or configured Timelock delay is not 600 s'),
            (exp.get('implementationRuntimeKeccak') == keccak256(bytes.fromhex(artifacts['distributor']['deployedBytecode'][2:])),
             'implementation runtime differs from the reviewed SlpDistributor artifact'),
            (exp.get('timelockRuntimeKeccak') == keccak256(bytes.fromhex(artifacts['timelock']['deployedBytecode'][2:])),
             'Timelock runtime differs from the reviewed TimelockController artifact'),
            (exp.get('proxyRuntimeKeccak') == (config.get('legacy', {}).get(addresses.get('distributor')) or {}).get('proxyRuntimeHash'),
             'proxy runtime differs from the reviewed OZ v4 TUP runtime'),
            (exp.get('proxyAdminRuntimeKeccak') == keccak256(bytes.fromhex(artifacts['proxyAdmin']['deployedBytecode'][2:])),
             'ProxyAdmin runtime differs from the reviewed ProxyAdmin artifact')):
        if not ok:
            out.append(why)
    return out + transaction_problems(p, inp, pred)


def transaction_problems(p, inp, pred):
    """Every byte of the five transactions, rebuilt from the plan inputs and the reviewed creation code:
    order, kind, sender, nonce, target, the full CREATE data (reviewed creation code + exact constructor arguments)
    and the exact call data; and every predicted address. Never decided by a suffix or by a runtime hash."""
    out = []
    txs = p.get('transactions')
    if not isinstance(txs, list) or [t.get('step') if isinstance(t, dict) else None for t in txs] != [1, 2, 3, 4, 5]:
        return ['plan does not have the five steps in order 1..5']
    dep, nonce = inp['deployer'], inp['deployerNonce']
    args = {2: timelock_args(inp['govSafe'], dep),
            5: proxy_args(pred['implementation'], inp['proxyAdmin'], pred['timelock'], inp.get('keeper'),
                          inp.get('destinations'), inp['guardian'])}
    creation = {1: 'SlpDistributor', 2: 'TimelockController', 5: 'tupV4'}
    calls = {3: abi.encode_function(_fn('grantRole', 'bytes32', 'address'), [ROLE['canceller'], inp['canSafe']]),
             4: abi.encode_function(_fn('renounceRole', 'bytes32', 'address'), [ROLE['admin'], dep])}
    created = {1: pred['implementation'], 2: pred['timelock'], 5: pred['proxy']}
    for t in txs:
        n, data = int(t['step']), t.get('data')
        if t.get('kind') != STEP_KINDS[n]:
            out.append(f'step {n}: kind is not {STEP_KINDS[n]}')
        if t.get('from') != dep or t.get('nonce') != nonce + n - 1:
            out.append(f'step {n}: sender or nonce differs from the plan inputs')
        if not isinstance(data, str):
            out.append(f'step {n}: no data')
            continue
        if n in creation:
            suffix = args.get(n, '')
            code = data[:len(data) - len(suffix)] if suffix else data
            if suffix and not data.endswith(suffix):
                out.append(f'step {n}: constructor arguments differ from the plan inputs')
            if not reviewed_creation(creation[n], code):
                out.append(f'step {n}: creation code is not the reviewed {creation[n]} creation code')
            if t.get('to') is not None:
                out.append(f'step {n}: a CREATE has no target')
            if t.get('predictedAddress') != created[n] or created[n] != create_address(dep, nonce + n - 1):
                out.append(f'step {n}: predicted address is not CREATE(deployer, nonce {nonce + n - 1})')
        else:
            if data != calls[n] or t.get('to') != pred['timelock']:
                out.append(f'step {n}: call differs from the plan inputs')
    return out
