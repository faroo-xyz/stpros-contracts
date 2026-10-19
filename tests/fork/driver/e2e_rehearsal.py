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

SAFE_OUT = ROOT / 'out' / 'SyntheticSafe.sol'
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


def safe_owners(chain_id, role, count=3):
    return sorted((address_of(chain_id, f'{role}.owner{i}') for i in range(count)), key=lambda a: int(a, 16))


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


def _artifact(name):
    path = SAFE_OUT / f'{name}.json'
    if not path.is_file():
        raise E2EError(f'forge output {path} missing: build the export first (forge build)')
    return json.loads(path.read_text())


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
def deploy_safe(h, role, owners, threshold):
    cid = h.ctx['chainId']
    singleton = getattr(h, 'rehearsal_singleton', None)
    deployer = address_of(cid, 'deployer')
    if singleton is None:
        singleton, _ = h.chain.create('E2E.rehearsal.safeSingleton', deployer, _artifact('SyntheticSafeSingleton')['bytecode']['object'])
        h.rehearsal_singleton = singleton
        _step(h, f'create SyntheticSafeSingleton {singleton}')
    initcode = _artifact('SyntheticSafeProxy')['bytecode']['object'] + abi.enc_word(singleton)
    safe, _ = h.chain.create(f'E2E.rehearsal.safe.{role}', deployer, initcode)
    _send(h, f'safe.{role}.setup', owners[0], safe, fn('setup(address[],uint256)', owners, threshold))
    _step(h, f'create {role} Safe {safe} ({threshold} of {len(owners)})')
    return _lower(safe)


def setup_safes(h, run, roles=ident.SAFE_ROLES, threshold=2):
    """Moves GOV/CAN/GRD/OPS/Treasury to Safes through one Timelock batch; the old holders lose their roles."""
    a, cid, tl = h.a, h.ctx['chainId'], h.a['timelock']
    old = dict(h.actors)
    safes = {role: deploy_safe(h, role, safe_owners(cid, role), threshold) for role in roles}
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
    """The record's ``safes`` section for the rehearsal Safes, as an approved record would carry it."""
    return {role: {'address': s, 'owners': states[role]['owners'], 'threshold': states[role]['threshold'],
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


def setup_integrations(h, run, directory, *, proxy_target=None):
    """RewardsVault + Proxy0 + channel proxy from the recorded runtimes; RewardsVault becomes the Oracle reporter and
    both proxies are whitelisted in the SlpDistributor (one Timelock batch). ``proxy_target`` overrides the
    RewardsVault forwarding pointer (negative control)."""
    a, cid = h.a, h.ctx['chainId']
    code = load_runtimes(directory)
    rv, p0, p1 = (address_of(cid, k) for k in ('rewardsVault', 'proxy0', 'proxyChannel'))
    admin, caller, operator = (address_of(cid, k) for k in ('integrationAdmin', 'rewardsCaller', 'proxyOperator'))
    init = 'initialize(address,address[],address,address)'
    h.chain.rpc('anvil_setCode', [rv, code['SlpPharosRewardsVault']])
    _send(h, 'rv.initialize', admin, rv, fn(init, admin, [caller], a['wpros'], a['oracle']))
    for p in (p0, p1):
        h.chain.rpc('anvil_setCode', [p, code['SlpPharosProxy']])
        _send(h, f'proxy.{p[:10]}.initialize', admin, p, fn(init, admin, [operator], a['bridge'], rv))
    _send(h, 'rv.setSlpPharosProxy', admin, rv, abi.calldata('setSlpPharosProxy(address)', proxy_target or p0))
    _step(h, f'setCode recorded RewardsVault {rv} and proxies {p0}, {p1}; initialized')
    timelock_batch(h, 'integrations', [(a['oracle'], abi.calldata('setSlp(address)', rv)),
                                       (a['distributor'], fn('setSlps(address[],bool)', [p0, p1], True))])
    h.ctx['actors']['reporter'] = h.chain.addr(a['oracle'], 'slp()')
    h.ctx['integrations'] = {'rewardsVault': _lower(rv), 'rewardsCaller': _lower(caller), 'rewardsTarget': _lower(p0),
                             'stakingProxies': [_lower(p0), _lower(p1)],
                             'proxyOperators': {_lower(p0): _lower(operator), _lower(p1): _lower(operator)}, 'approved': False}
    return h.ctx['integrations']
