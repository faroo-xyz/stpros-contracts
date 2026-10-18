"""Companion adapter: the accepted history and observation outputs -> the native release inventory. Test-only.

For a future authorized run the release tooling's inventory (``tools/release/native/inputs.validate_inventory``)
must come from the content-verified history, not from a local node. This adapter takes exactly the accepted
boundary objects and changes none of them:

- ``verified``: the result of ``history.verify_manifest`` (through ``collector.content_verify``), whose rows are
  the content-verified log set;
- ``derived``: ``history_inventory.derive(verified, chain)``, the accepted normalizer, used as-is;
- ``observation_v1``: the v1 observation that ``history_inventory.check_observation`` accepted at the anchor.

Each section is rebuilt from the verified rows and reconciled with the accepted derivation and the observation:
a role key is ``current`` only if the replay says so *and* the observation's ``hasRole`` agrees; a holder balance
must equal both the derivation and the observation's ``balanceOf``; Σ pending from ``Withdraw`` /
``WithdrawalCompleted`` must equal ``queued - completed`` observed. Any disagreement is ``FAIL`` with the
mismatch. A role target without history (the new Timelock, which P-0 deploys after the anchor) is INCOMPLETE:
its creation-to-fork history must be collected as a new target (docs/UPGRADES.md), never assumed empty.

The output is never ``complete: true`` unless every section is PASS; the release tooling refuses anything else.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
from localchain import address  # noqa: E402
from release_config import TOPICS  # noqa: E402
from tools.lib import abi  # noqa: E402


class InventoryIncomplete(ValueError):
    """The accepted inputs do not support a complete release inventory."""


def _block(r):
    return int(r['blockNumber'], 16)


def roles(verified, derived, observation_v1, target):
    rows = [r for r in verified['rows'] if r['address'] == target]
    events, current, ever = [], set(), {}
    for r in rows:
        if r['topics'][0] not in (TOPICS['RoleGranted'], TOPICS['RoleRevoked']):
            continue
        key = f"{r['topics'][1]}:{address(r['topics'][2])}"
        ever[key] = None
        (current.add if r['topics'][0] == TOPICS['RoleGranted'] else current.discard)(key)
        events.append({'event': 'RoleGranted' if r['topics'][0] == TOPICS['RoleGranted'] else 'RoleRevoked',
                       'role': r['topics'][1], 'account': address(r['topics'][2]), 'block': _block(r),
                       'logIndex': int(r['logIndex'], 16), 'transactionHash': r['transactionHash']})
    accepted = derived['roles'].get(target, {'holders': {}, 'adminChanges': 0})
    accepted_current = {f'{role}:{a}' for role, accounts in accepted['holders'].items() for a in accounts}
    observed = (observation_v1.get('roles') or {}).get(target, {})
    mismatches = sorted(current ^ accepted_current)
    # every ever-held key must have been observed, and every observed probe (including the reviewed candidate
    # accounts the history never saw) must agree with the replay: a role held without an event is a FAIL
    probes = {f'{role}:{account}': seen for role, accounts in observed.items() for account, seen in accounts.items()}
    for key in sorted(set(ever) | set(probes)):
        seen = probes.get(key)
        if seen is None or bool(seen) != (key in current):
            mismatches.append(f'observation {key}: {seen}')
    status = 'PASS' if not mismatches and not accepted.get('adminChanges') else 'FAIL'
    return {'status': status, 'mismatches': mismatches, 'adminChanges': [] if not accepted.get('adminChanges') else
            ['RoleAdminChanged present: requires review'], 'events': events, 'current': sorted(current), 'ever': sorted(ever)}


def holders(derived, observation_v1, token):
    accepted = derived['holders'][token]['balances']
    observed = observation_v1['tokens'][token]
    mismatches = [{'account': a, 'derived': v, 'observed': observed['balanceOf'].get(a)}
                  for a, v in sorted(accepted.items()) if observed['balanceOf'].get(a) != v]
    total = sum(int(v) for v in accepted.values())
    if str(total) != observed['totalSupply']:
        mismatches.append({'sumDerived': str(total), 'totalSupply': observed['totalSupply']})
    return {'status': 'PASS' if not mismatches else 'FAIL', 'mismatches': mismatches, 'totalSupply': observed['totalSupply'],
            'accounts': [{'address': a, 'balance': v} for a, v in sorted(accepted.items()) if int(v) != 0]}


def withdrawals(verified, observation_v1, stpros):
    requested = completed = 0
    for r in verified['rows']:
        if r['address'] != stpros:
            continue
        if r['topics'][0] == TOPICS['Withdraw']:
            requested += abi.dec_uint(r['data'], 0)
        elif r['topics'][0] == TOPICS['WithdrawalCompleted']:
            completed += int(r['data'], 16)
    w = observation_v1['withdrawals']
    ok = str(requested) == w['queued'] and str(completed) == w['completed']
    return {'status': 'PASS' if ok else 'FAIL', 'sumPending': str(requested - completed), 'queued': w['queued'],
            'completed': w['completed']}


def build(verified, derived, observation_v1, chain, timelock):
    """The release inventory for ``timelock`` plus every derived vault. Raises ``InventoryIncomplete`` unless every
    section reconciles; the partial document is attached to the exception for review."""
    if observation_v1.get('block') != derived['anchorBlock'] or observation_v1.get('blockHash') != derived['anchorBlockHash']:
        raise InventoryIncomplete('observation is not at the history anchor')
    reg = h.registry(chain)
    vaults = [v for vs in derived['factories'].values() for v in vs]
    doc = {'complete': False, 'startCodeAbsent': True, 'chainId': h.CHAINS[chain], 'fromBlock': derived['fromBlock'],
           'toBlock': derived['anchorBlock'], 'blockHash': derived['anchorBlockHash'], 'windows': _windows(derived),
           'roles': {v: roles(verified, derived, observation_v1, v) for v in vaults},
           'holders': {t: holders(derived, observation_v1, t) for t in [reg['stpros'], *vaults]},
           'withdrawals': withdrawals(verified, observation_v1, reg['stpros'])}
    timelock_rows = [r for r in verified['rows'] if r['address'] == timelock]
    gaps = []
    if not timelock_rows:
        doc['roles'][timelock] = {'status': 'INCOMPLETE', 'mismatches': [], 'adminChanges': [], 'events': [],
                                  'current': [], 'ever': []}
        gaps.append(f'{timelock}: Timelock history not collected (new target after P-0)')
    else:
        doc['roles'][timelock] = roles(verified, derived, observation_v1, timelock)
    for kind in ('roles', 'holders'):
        gaps += [f'{kind} {t}: {s["status"]}' for t, s in doc[kind].items() if s['status'] != 'PASS' and t != timelock]
    if doc['withdrawals']['status'] != 'PASS':
        gaps.append('withdrawals: FAIL')
    if hi.reviewed_vault_runtimes(chain) is None:
        gaps.append('no reviewed vault runtimes')
    if gaps:
        error = InventoryIncomplete('; '.join(gaps))
        error.document = doc
        raise error
    doc['complete'] = True
    return doc


def _windows(derived):
    out, start = [], derived['fromBlock']
    while start <= derived['anchorBlock']:
        out.append({'from': start, 'to': min(start + 999, derived['anchorBlock'])})
        start += 1000
    return out
