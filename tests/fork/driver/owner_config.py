"""Configuration changes made after the B0 pin and how a real-state run may use them. Test-only.

The archived state check compares the testnet Oracle reporter with the reviewed registry and fails (``REPORTER_REGISTRY``),
because the testnet owner changed it after the pin. That failure stays in the archived evidence. This module records
each such change and decides, for one observation, whether its current value may serve as an expectation:

- ``CHANGES``: one entry per (chain, contract, getter): the exact change event (block, block hash, log index,
  transaction, old and new value), the anchor at which the new value was observed, and its standing:
  ``OWNER_CONFIRMED`` (an explicit owner decision), ``UPGRADE_TARGET`` (the value the R4 end-state assertion
  itself requires, here the SlpDistributor as the StPROS sink) or ``OWNER_DECISION_PENDING`` (technically verified only).
- ``check``: per entry, ``MATCH`` only when the observation is on the entry's chain and at its anchor, the getter
  equals the recorded new value, and the last change event of that getter in the content-verified history is exactly
  the recorded event. Anything else is ``CHAIN_MISMATCH``, ``ANCHOR_MISMATCH``, ``DRIFT``, ``LATER_CHANGE`` or
  ``EVENT_MISMATCH``.
- ``expected``: the value a release configuration may take as its expectation. Only a ``MATCH`` of an
  ``OWNER_CONFIRMED`` or ``UPGRADE_TARGET`` entry yields one; a pending entry is BLOCKED and a mismatch is FAIL. An
  observed value is never its own approval.
- ``dispositions``: annotations for the archived ``REPORTER_REGISTRY`` failures. The report itself is not changed.

Nothing here reads the network. This file is outside every FR-D1 source binding, like ``fr_d1_approvals.py``: an
entry is added only by a separate reviewed change that names the owner decision.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history as h  # noqa: E402
from tools.lib.keccak import keccak256_bytes  # noqa: E402

SCHEMA = 'faroo-owner-config-check-v1'
STANDINGS = ('OWNER_CONFIRMED', 'UPGRADE_TARGET', 'OWNER_DECISION_PENDING')
SLP_CHANGED = '0x' + keccak256_bytes(b'SlpChanged(address,address)').hex()
KEEPER_CHANGED = '0x' + keccak256_bytes(b'KeeperChanged(address,address)').hex()
TESTNET_ANCHOR = {'block': 31_580_881, 'hash': '0x8727295552e06aa735c8f7de00410a8f9bdac627e333136ef33d58f584b76555'}

CHANGES = (
    {'id': 'testnet-oracle-reporter', 'chain': 'testnet', 'chainId': 688689, 'section': 'oracle', 'field': 'slp',
     'contract': '0x6bd39d03d2fbbf14aee362977a09c293b282b0bc', 'getter': 'slp()', 'role': 'Oracle reporter (slp)',
     'old': '0x464017cdc3c2af2b5b525fde03ac93f15172db43', 'new': '0xe45493245c4bc3a573d35b191d8674f861e1ef45',
     'event': {'topic0': SLP_CHANGED, 'block': 31_195_028, 'logIndex': 0,
               'blockHash': '0x475a4c6c64b8c27a9d815164b860a8f67fb365949c99920b3062e55a3b1a6174',
               'transactionHash': '0x71b823e7f7dc836d7da95419a043aa04b114df88f619db3afc770f2088ebb0b4'},
     'anchor': TESTNET_ANCHOR, 'standing': 'OWNER_CONFIRMED',
     'decision': 'Owner decision of 2026-10-19 (Agent B task message): the testnet Oracle configuration was changed '
                 'deliberately; the confirmed reporter is the new value. Testnet Oracle only; not a mainnet policy and '
                 'not an approval of any later change'},
    {'id': 'testnet-stpros-sink', 'chain': 'testnet', 'chainId': 688689, 'section': 'stpros', 'field': 'slp',
     'contract': '0xb1437aeea18189eb6d02dc46cd4d28613d582e9a', 'getter': 'slp()', 'role': 'StPROS deposit sink (slp)',
     'old': '0x464017cdc3c2af2b5b525fde03ac93f15172db43', 'new': '0x008a7856fecf9a73f5011f77b020877f5d74a7f2',
     'event': {'topic0': SLP_CHANGED, 'block': 31_195_019, 'logIndex': 0,
               'blockHash': '0x8dc31ae5812efd26e7229c8f5f8a636759491979ef4e560dc5d9f22f9963361f',
               'transactionHash': '0x204cf14d32b4dce597c4cd11b31c3e2e81fef27feded601023449e615e99faa3'},
     'anchor': TESTNET_ANCHOR, 'standing': 'UPGRADE_TARGET',
     'decision': 'The R4 end-state assertion requires StPROS.slp() == the SlpDistributor proxy (ReleaseAssertions '
                 '"SINK"); the new value is the registry SlpDistributor'},
    {'id': 'testnet-distributor-keeper', 'chain': 'testnet', 'chainId': 688689, 'section': 'slpDistributor',
     'field': 'keeper', 'contract': '0x008a7856fecf9a73f5011f77b020877f5d74a7f2', 'getter': 'keeper()',
     'role': 'SlpDistributor keeper', 'old': '0x0000000000000000000000000000000000000000',
     'new': '0x2fa913f7637fb691ce3bf6d2bae8502bd2b1914b',
     'event': {'topic0': KEEPER_CHANGED, 'block': 31_195_033, 'logIndex': 0,
               'blockHash': '0xab812ac699f54c8a318d5e1af51ff7677a5f45f76e7864913d8dfdea72340029',
               'transactionHash': '0x55fe9d9f911d9d0958d30a42fcd3368048df0df5e6a8f5fe345553a322d5549b'},
     'anchor': TESTNET_ANCHOR, 'standing': 'OWNER_DECISION_PENDING',
     'decision': 'Technically verified only (owner-only setter, recorded event, getter agrees). The owner has not '
                 'confirmed this keeper'},
)


class OwnerConfigRefused(ValueError):
    def __init__(self, code, detail='', status='FAIL'):
        super().__init__(f'{status}: {code}: {detail}')
        self.code, self.detail, self.status = code, detail, status


def _topic_address(topic):
    return '0x' + topic[-40:].lower()


def _key(row):
    return int(row['blockNumber'], 16), int(row['logIndex'], 16)


def observed_values(state_doc):
    """{(section, field): value} for the getters ``CHANGES`` covers, from a ``faroo-fork-state-observation-v1``
    document or from equivalent Fork reads."""
    out = {}
    for entry in CHANGES:
        section = state_doc.get(entry['section'])
        if isinstance(section, dict) and isinstance(section.get(entry['field']), str):
            out[(entry['section'], entry['field'])] = section[entry['field']].lower()
    return out


def _check_entry(entry, chain, anchor, rows, observed):
    if chain != entry['chain'] or h.CHAINS.get(chain) != entry['chainId']:
        return 'CHAIN_MISMATCH', f'{chain} is not {entry["chain"]}'
    if (anchor.get('block'), anchor.get('hash')) != (entry['anchor']['block'], entry['anchor']['hash']):
        return 'ANCHOR_MISMATCH', f'anchor {anchor.get("block")} is not the recorded {entry["anchor"]["block"]}'
    value = observed.get((entry['section'], entry['field']))
    if value != entry['new']:
        return 'DRIFT', f'{entry["role"]}: observed {value}, recorded {entry["new"]}'
    events = sorted((r for r in rows if r['address'] == entry['contract'] and r['topics']
                     and r['topics'][0] == entry['event']['topic0'] and _key(r)[0] <= anchor['block']), key=_key)
    if not events:
        return 'EVENT_MISMATCH', 'no change event in the history'
    last, e = events[-1], entry['event']
    exact = (_key(last) == (e['block'], e['logIndex']) and last['blockHash'] == e['blockHash']
             and last['transactionHash'] == e['transactionHash'] and len(last['topics']) == 3
             and _topic_address(last['topics'][1]) == entry['old'] and _topic_address(last['topics'][2]) == entry['new'])
    if exact:
        return 'MATCH', 'getter, last change event and recorded change agree'
    if any(_key(r) == (e['block'], e['logIndex']) for r in events[:-1]):
        return 'LATER_CHANGE', f'a later change at block {_key(last)[0]} follows the recorded one'
    return 'EVENT_MISMATCH', f'the last change event (block {_key(last)[0]}) is not the recorded one'


def check(chain, anchor, rows, observed):
    """One result per ``CHANGES`` entry of ``chain`` (an empty list for a chain with none)."""
    return [{'id': entry['id'], 'standing': entry['standing'], 'role': entry['role'],
             'result': result, 'detail': detail}
            for entry in CHANGES if entry['chain'] == chain
            for result, detail in [_check_entry(entry, chain, anchor, rows, observed)]]


def expected(entry_id, chain, anchor, rows, observed):
    """The approved value of one entry for a release configuration, or a refusal."""
    entry = next((e for e in CHANGES if e['id'] == entry_id), None)
    if entry is None:
        raise OwnerConfigRefused('NOT_RECORDED', entry_id)
    result, detail = _check_entry(entry, chain, anchor, rows, observed)
    if result != 'MATCH':
        raise OwnerConfigRefused(result, detail)
    if entry['standing'] == 'OWNER_DECISION_PENDING':
        raise OwnerConfigRefused('OWNER_DECISION_PENDING', entry['role'], 'BLOCKED')
    return entry['new']


def dispositions(report, results):
    """Annotations for the archived ``REPORTER_REGISTRY`` failures; the report is read, never changed."""
    by_id = {r['id']: r for r in results}
    out = []
    for issue in report['sections'].get('oracle', {}).get('issues', []):
        if issue['rule'] != 'REPORTER_REGISTRY':
            continue
        r = by_id.get('testnet-oracle-reporter')
        resolved = r is not None and r['result'] == 'MATCH' and r['standing'] == 'OWNER_CONFIRMED'
        out.append({'rule': 'REPORTER_REGISTRY', 'originalSeverity': issue['severity'], 'originalDetail': issue['detail'],
                    'disposition': 'OWNER-CONFIRMED CONFIGURATION CHANGE' if resolved else 'UNRESOLVED',
                    'check': r})
    return out
