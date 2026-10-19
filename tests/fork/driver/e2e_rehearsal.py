"""Synthetic rehearsals of the real-state E2E inputs. Test-only.

These functions prepare a synthetic loopback node so that the harness paths a real-state Fork needs can be
exercised offline:
- the testnet Safe route: GOV/CAN/GRD/OPS/Treasury held by Safes and driven through ``safe_exec``;
- the RewardsVault / staking-proxy integration endpoints (C02 via ``notifyOracle``, C04 transferBack).

What they install:
- the Safes are the local models of ``tests/fork/synthetic/SyntheticSafe.sol``, deployed from this export's forge
  output;
- the integration contracts are the **recorded testnet runtimes** of ``SlpPharosRewardsVault`` and
  ``SlpPharosProxy`` (sha-pinned files staged through ``FAROO_E2E_SLP_RUNTIMES``), placed with ``anvil_setCode`` at
  rehearsal addresses and initialized locally.

Every step is recorded in ``ctx['rehearsal']``. That list keeps the formal verdict BLOCKED, and it BLOCKS a context
that claims REAL evidence (``Harness.begin``). These functions prove harness wiring; they prove nothing about the
real Safes (FR-X8) or the real integration contracts, which remain outside the V4 scope.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from e2e import E2EError, _lower  # noqa: E402
import e2e_identity as ident  # noqa: E402
import safe_governance as sg  # noqa: E402

SAFE_OUT = sg.SAFE_OUT
# Raw SHA-256 of the recorded deployment files (vETH-3.0-contract, deployments/pharos_atlantic_testnet).
RUNTIMES = {
    'SlpPharosRewardsVault_Implementation.json': '43e5770274eb82656df50f201d72c2855e963c6b92dbbf8468d49c9165c8c53e',
    'SlpPharosProxy_Implementation.json': '5bf4e66720654d87b70ae3d0864ab495b6d566d10bc75ed34894ba1dfa0d8861',
}
WHITELIST = '0x' + keccak256(b'WHITELIST_ROLE')[2:]
ADMIN = '0x' + '00' * 32
CORE = ('stpros', 'oracle', 'bridge', 'distributor', 'factory')


def address_of(chain_id, label):
    """Deterministic rehearsal address (tests can predict it before the run)."""
    return '0x' + keccak256(f'faroo e2e rehearsal {chain_id} {label}'.encode())[-40:]


def runtimes_dir():
    value = os.environ.get('FAROO_E2E_SLP_RUNTIMES')
    return Path(value) if value else None


def fn(signature, *values):
    """Calldata for a plain signature (static types and arrays of them)."""
    name, args = signature.split('(', 1)
    types = [t for t in args[:-1].split(',') if t]
    return abi.encode_function({'type': 'function', 'name': name, 'inputs': [{'type': t} for t in types]}, list(values))


def _step(h, what):
    h.ctx.setdefault('rehearsal', []).append(what)


def _send(h, label, sender, to, data):
    receipt, rec = h.chain.send(f'E2E.rehearsal.{label}', sender, to, data, expect=True)
    if rec['status'] != 1:
        raise E2EError(f'rehearsal step {label} failed')
    return receipt


def timelock_batch(h, label, calls):
    """One Timelock batch through the current governance route (scheduleBatch, full delay, executeBatch)."""
    tl, g = h.a['timelock'], h.ctx['governance']
    targets, values, datas = [c[0] for c in calls], [0] * len(calls), [c[1] for c in calls]
    salt = '0x' + keccak256(f'faroo e2e rehearsal {label}'.encode())[2:]
    sched = h.calldata('TimelockController', 'scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)',
                       targets, values, datas, ADMIN, salt, g['delay'])
    execute = h.calldata('TimelockController', 'executeBatch(address[],uint256[],bytes[],bytes32,bytes32)',
                         targets, values, datas, ADMIN, salt)
    h._gov_send(f'rehearsal.{label}.schedule', tl, sched, True)
    h.chain.warp(g['delay'] + 1)
    h._gov_send(f'rehearsal.{label}.execute', tl, execute, True)
    _step(h, f'TL batch {label}: {len(calls)} calls')


# ------------------------------------------------------------------------------------------------ Safes
def setup_safes(h, run, roles=ident.SAFE_ROLES):
    """Moves GOV/CAN/GRD/OPS/Treasury to local Safe models (release-mode profile ``safe_governance.PROFILE``: GOV 3/5,
    CAN 3/5, disjoint signers) through one Timelock batch; the old holders lose their roles."""
    a, cid, tl = h.a, h.ctx['chainId'], h.a['timelock']
    old = dict(h.actors)
    specs = sg.deploy_model(h.chain, f'e2e-rehearsal-{cid}', roles)
    safes = {role: spec['address'] for role, spec in specs.items()}
    _step(h, f'create local Safe models {sorted(safes)}')
    calls = []
    role_call = lambda target, fn, role, who: (target, abi.calldata(f'{fn}(bytes32,address)', role, who))
    if 'gov' in safes:
        for r in ('PROPOSER', 'EXECUTOR'):
            calls.append(role_call(tl, 'grantRole', ident.TL_ROLES[r], safes['gov']))
    if 'can' in safes:
        calls.append(role_call(tl, 'grantRole', ident.TL_ROLES['CANCELLER'], safes['can']))
    if 'guardian' in safes:
        calls += [(a[k], abi.calldata('setGuardian(address)', safes['guardian'])) for k in CORE]
    if 'ops' in safes:
        for v in a['governedVaults']:
            if not h.chain.boolean(v, 'hasRole(bytes32,address)', ADMIN, tl):
                raise E2EError(f'governed vault {v} is not administered by the Timelock')
            calls.append(role_call(v, 'grantRole', ident.OPERATOR, safes['ops']))
            calls.append(role_call(v, 'revokeRole', ident.OPERATOR, old['ops']))
    if 'treasury' in safes:
        rate = h.u(a['oracle'], 'commissionRatePpm()')
        calls.append((a['oracle'], abi.calldata('setCommissionConfig(address,uint256)', safes['treasury'], rate)))
    # the old GOV / CAN holders give up their Timelock roles in the same batch (checked at executeBatch entry)
    for key, names in (('gov', ('PROPOSER', 'EXECUTOR', 'CANCELLER')), ('can', ('CANCELLER',))):
        if key in safes and h.ctx['governance']['mode'] == 'EOA':
            for r in names:
                if h.chain.boolean(tl, 'hasRole(bytes32,address)', ident.TL_ROLES[r], old[key]):
                    calls.append(role_call(tl, 'revokeRole', ident.TL_ROLES[r], old[key]))
    timelock_batch(h, 'safes', calls)
    states = {role: ident.safe_state(h.chain, s) for role, s in safes.items()}
    h.ctx['safes'] = {s: {'role': role, 'owners': states[role]['owners'], 'threshold': states[role]['threshold']}
                      for role, s in safes.items()}
    h.ctx['actors'].update(safes)
    if 'gov' in safes:
        h.ctx['governance'] = {'mode': 'SAFE', 'safe': safes['gov'], 'owners': states['gov']['owners'],
                               'threshold': states['gov']['threshold'], 'delay': h.ctx['governance']['delay'],
                               'source': 'rehearsal Safe (synthetic model), read from the node'}
    return safes, states


def safe_entries(safes, states, manifest_sha='0x' + '00' * 32):
    """The record's ``safes`` section for the rehearsal Safes, as an approved record would carry it (the release
    tool's ``review.safes`` identity fields)."""
    return {role: {'address': s, 'owners': states[role]['owners'], 'threshold': states[role]['threshold'],
                   'runtimeHash': states[role]['runtimeHash'], 'singleton': states[role]['singleton'],
                   'singletonHash': states[role]['singletonHash'], 'modules': [], 'guard': '0x' + '00' * 20,
                   'safeManifestSha256': manifest_sha} for role, s in safes.items()}


# ------------------------------------------------------------------------------------------ integrations
def load_runtimes(directory):
    out = {}
    for name, sha in RUNTIMES.items():
        path = Path(directory) / name
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise E2EError(f'recorded runtime {name}: SHA-256 differs from the pinned {sha[:16]}')
        out[name.split('_')[0]] = json.loads(raw)['deployedBytecode']
    return out


def create_address(sender, nonce):
    """CREATE address of ``sender`` at ``nonce`` (RLP of [sender, nonce]; nonce < 128)."""
    raw = bytes([0xd6, 0x94]) + bytes.fromhex(sender[2:]) + (bytes([nonce]) if nonce else b'\x80')
    return '0x' + keccak256(raw)[-40:]


def _transparent_proxy(h, label, deployer, implementation, owner, init):
    """The canonical (V4 build) OZ 5 TransparentUpgradeableProxy over ``implementation``. Its constructor creates the
    ProxyAdmin at CREATE nonce 1 of the proxy: the expected admin is derived from that rule, not read back."""
    from localchain import canonical
    art = canonical('TransparentUpgradeableProxy')
    if art.get('creationLinks'):
        raise E2EError('TransparentUpgradeableProxy creation bytecode has unresolved links')
    ctor = next(i for i in art['abi'] if i['type'] == 'constructor')
    args = abi.encode_parameters(ctor['inputs'], [implementation, owner, init])
    proxy, _ = h.chain.create(f'E2E.rehearsal.{label}', deployer, art['creationBytecode'] + args[2:])
    return _lower(proxy), create_address(_lower(proxy), 1)


def setup_integrations(h, run, directory, *, proxy_target=None):
    """RewardsVault + Proxy0 + channel proxy: the recorded runtimes as implementations behind EIP-1967 transparent
    proxies, initialized locally. The RewardsVault becomes the Oracle reporter and both proxies are whitelisted in
    the SlpDistributor (one Timelock batch). ``proxy_target`` overrides the RewardsVault forwarding pointer (negative
    control). The deployment evidence (implementation, admin, implementation runtime hash) is derived independently
    and handed to C04 as ``proxyDeployments``."""
    a, cid = h.a, h.ctx['chainId']
    code = load_runtimes(directory)
    impl_rv, impl_p = address_of(cid, 'rewardsVaultImpl'), address_of(cid, 'slpProxyImpl')
    admin, caller, operator, deployer = (address_of(cid, k) for k in ('integrationAdmin', 'rewardsCaller', 'proxyOperator',
                                                                      'integrationDeployer'))
    init = 'initialize(address,address[],address,address)'
    h.chain.rpc('anvil_setCode', [impl_rv, code['SlpPharosRewardsVault']])
    h.chain.rpc('anvil_setCode', [impl_p, code['SlpPharosProxy']])
    rv, _ = _transparent_proxy(h, 'rewardsVault', deployer, impl_rv, admin, fn(init, admin, [caller], a['wpros'], a['oracle']))
    deployments = {}
    proxies = []
    for label in ('proxy0', 'proxyChannel'):
        # initialized in the constructor (this OZ 5 ERC1967Proxy refuses empty data: ERC1967ProxyUninitialized)
        p, proxy_admin = _transparent_proxy(h, label, deployer, impl_p, admin, fn(init, admin, [operator], a['bridge'], rv))
        proxies.append(p)
        deployments[p] = {'implementation': _lower(impl_p), 'admin': proxy_admin,
                          'implementationRuntimeHash': keccak256(bytes.fromhex(code['SlpPharosProxy'][2:]))}
    p0, p1 = proxies
    _send(h, 'rv.setSlpPharosProxy', admin, rv, abi.calldata('setSlpPharosProxy(address)', proxy_target or p0))
    _step(h, f'setCode recorded runtimes at {impl_rv}, {impl_p}; transparent proxies {rv}, {p0}, {p1}; initialized')
    timelock_batch(h, 'integrations', [(a['oracle'], abi.calldata('setSlp(address)', rv)),
                                       (a['distributor'], fn('setSlps(address[],bool)', [p0, p1], True))])
    h.ctx['actors']['reporter'] = h.chain.addr(a['oracle'], 'slp()')
    h.ctx['integrations'] = {'rewardsVault': _lower(rv), 'rewardsCaller': _lower(caller), 'rewardsTarget': _lower(p0),
                             'stakingProxies': [p0, p1], 'proxyOperators': {p0: _lower(operator), p1: _lower(operator)},
                             'proxyDeployments': deployments, 'approved': False}
    return h.ctx['integrations']
