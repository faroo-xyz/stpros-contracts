"""XF-02: the off-chain authority evidence gate immediately before H. Test-only.

OpenZeppelin AccessControl is not enumerable, and this gate does not pretend otherwise. It bounds what can be
unknown at the gate block ``G`` with three pieces of evidence, each with a stated limit:

1. **Pin inventory.** The role holders at the reviewed inventory block ``P`` come from the accepted history replay
   (history-known keys only). A key the history never saw is not in it.
2. **Authority event delta.** Every log of every reviewed target in ``(P, G]`` whose topic is a role, ownership,
   admin, beacon or upgrade event must be one of the explicitly reviewed events. Any ``grantRole`` on unmodified
   OpenZeppelin code emits ``RoleGranted`` whoever the recipient is, so a grant to a never-seen account after the pin
   is caught here (and only here: ``assertHandoff`` checks history-known keys).
3. **Code identity.** The runtime code hash, the EIP-1967 implementation and admin slots and, for beacon proxies, the
   beacon implementation of every target at ``G`` equal the reviewed values at ``P``. A role write without an event
   needs code the reviewed runtimes do not contain (an upgrade, or a delegatecall into foreign code), and such a
   change is caught here.

Finally, ``hasRole`` is probed at ``G`` for every history-known key and every account named by a reviewed delta event,
and must agree with the replay.

**Residual (stated, not closed).** An eventless storage write that reaches a role mapping without any code change is
outside the gate. On the reviewed runtimes no such path exists; locally it is produced only by a test cheat
(``anvil_setStorageAt``). Test-only perturbations of this kind are recorded as the boundary, never as a detection.
The gate needs authentic logs for ``(P, G]`` (FH-D2 for real state); here the logs are the loopback node's own.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from localchain import address  # noqa: E402
import models  # noqa: E402
from models import truth  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native._shared import SLOTS  # noqa: E402

AUTHORITY_EVENTS = {keccak256(sig.encode()): sig for sig in (
    'RoleGranted(bytes32,address,address)', 'RoleRevoked(bytes32,address,address)',
    'RoleAdminChanged(bytes32,bytes32,bytes32)', 'OwnershipTransferred(address,address)',
    'OwnershipTransferStarted(address,address)', 'Upgraded(address)', 'AdminChanged(address,address)',
    'BeaconUpgraded(address)', 'GuardianChanged(address,address)', 'TimelockChanged(address,address)')}
BEACON_SLOT = '0x' + format(int(keccak256(b'eip1967.proxy.beacon'), 16) - 1, '064x')


def identity(chain, target, block='latest'):
    """Runtime hash, implementation, admin and beacon (with its implementation) of ``target`` at ``block``."""
    out = {'runtime': keccak256(bytes.fromhex(chain.code(target, block)[2:])),
           'implementation': address(chain.slot(target, SLOTS['implementation'], block)),
           'admin': address(chain.slot(target, SLOTS['admin'], block)),
           'beacon': address(chain.slot(target, BEACON_SLOT, block))}
    if out['beacon'] != '0x' + '00' * 20:
        raw = chain.rpc('eth_call', [{'to': out['beacon'], 'data': '0x5c60da1b'}, block])     # implementation()
        out['beaconImplementation'] = address(raw)
        out['beaconRuntime'] = keccak256(bytes.fromhex(chain.code(out['beacon'], block)[2:]))
    return out


def reviewed_identities(chain, targets, pin_block):
    return {t: identity(chain, t, hex(pin_block)) for t in targets}


def gate(chain, prefix, targets, pin_block, history_rows, reviewed, reviewed_events=()):
    """The three-part gate at the latest block. Returns ``(checks, problems)``; ``problems`` empty = gate passes.
    ``history_rows`` is the authenticated history up to ``pin_block``; ``reviewed`` the identities at the pin;
    ``reviewed_events`` the (transactionHash, logIndex) pairs explicitly reviewed for ``(P, G]``."""
    checks, problems = [], []
    allowed = {(h.lower(), int(i, 16) if isinstance(i, str) else i) for h, i in reviewed_events}
    delta = [r for r in chain.logs(sorted(targets), pin_block + 1) if r['topics'][0] in AUTHORITY_EVENTS]
    unreviewed = [r for r in delta if (r['transactionHash'].lower(), int(r['logIndex'], 16)) not in allowed]
    for r in unreviewed:
        problems.append(f"UNREVIEWED_AUTHORITY_EVENT {AUTHORITY_EVENTS[r['topics'][0]].split('(')[0]} on {r['address']}")
    checks.append(truth(f'{prefix}.eventDelta.reviewed', not unreviewed, '; '.join(problems)[:300]))
    for t in sorted(targets):
        now = identity(chain, t)
        if now != reviewed[t]:
            changed = sorted(k for k in now if now.get(k) != reviewed[t].get(k))
            problems.append(f'CODE_IDENTITY {t} {changed}')
        checks.append(truth(f'{prefix}.codeIdentity.{t[:10]}', now == reviewed[t]))
    pin_rows = [r for r in history_rows if int(r['blockNumber'], 16) <= pin_block]
    for t in sorted(targets):
        keys = {f"{r['topics'][1]}:{address(r['topics'][2])}" for r in pin_rows
                if r['address'] == t and r['topics'][0] in (models.TOPICS['RoleGranted'], models.TOPICS['RoleRevoked'])}
        keys |= {f"{r['topics'][1]}:{address(r['topics'][2])}" for r in delta
                 if r['address'] == t and r['topics'][0] in (models.TOPICS['RoleGranted'], models.TOPICS['RoleRevoked'])}
        expected = models.replay_roles(pin_rows + [r for r in delta if r['address'] == t], t)
        wrong = [k for k in sorted(keys) if chain.boolean(t, 'hasRole(bytes32,address)', k[:66], k[67:]) != (k in expected)]
        if wrong:
            problems.append(f'ROLE_PROBE {t} {wrong[:2]}')
        checks.append(truth(f'{prefix}.probe.{t[:10]}', not wrong, str(wrong[:2])))
    return checks, problems
