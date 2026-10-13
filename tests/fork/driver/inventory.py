"""Normalizes an authenticated B0-format inventory into ``faroo-fork-inventory-v1``.

Test-only. The inventory directory is always passed explicitly together with the SHA-256 of
every file that is read; Production never locates the Research repository by itself. The
normalizer recomputes the aggregates it relies on (Σ holder balances, Σ pending, Q − C) from the
per-account data and refuses the inventory if they disagree with the recorded relations. The
output is the independent seed of the ghost models; it never contains a value read from a
contract under test.
"""
import hashlib
import json
from pathlib import Path
import re

SCHEMA = 'faroo-fork-inventory-v1'
FILES = ('holders.json', 'withdrawals.json', 'roles.json', 'owners.json', 'summary.json')
CHAINS = {'mainnet': 1672, 'testnet': 688689}
_ADDRESS = re.compile(r'0x[0-9a-f]{40}')
_SHA = re.compile(r'[0-9a-f]{64}')
ZERO_ADDRESS = '0x' + '0' * 40


class InventoryRefused(ValueError):
    """The inventory is unauthenticated, incomplete or internally inconsistent."""


def _require(condition, message):
    if not condition:
        raise InventoryRefused(message)


def _uint(text, where):
    _require(isinstance(text, str) and text.isdigit(), f'{where} must be a decimal string')
    return int(text)


def _addr(text, where):
    _require(isinstance(text, str) and _ADDRESS.fullmatch(text.lower()) is not None, f'{where} must be an address')
    return text.lower()


def read_authenticated(directory, expected_sha256):
    """Reads exactly FILES from `directory`; every file must match its expected SHA-256."""
    directory = Path(directory)
    _require(set(expected_sha256) == set(FILES), f'expected hashes must cover exactly {list(FILES)}')
    docs = {}
    for name in FILES:
        digest = expected_sha256[name]
        _require(isinstance(digest, str) and _SHA.fullmatch(digest) is not None, f'{name}: invalid expected hash')
        raw = (directory / name).read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == digest, f'{name}: SHA-256 mismatch (unauthenticated inventory)')
        docs[name] = json.loads(raw)
    return docs


def normalize(docs, chain, stpros):
    """Builds the normalized StPROS inventory. `stpros` is the expected StPROS proxy address."""
    _require(chain in CHAINS, 'chain')
    stpros = _addr(stpros, 'stpros')
    summary = docs['summary.json']
    _require(summary['chain_id'] == CHAINS[chain], 'summary chain id')
    _require(summary['coverage'] == 'COMPLETE' and not summary['coverage_problems'], 'INCOMPLETE: log coverage')
    _require(all(c.get('status') == 'PASS' for c in summary['checks'].values()), 'summary checks not all PASS')
    anchor = summary['pinned_block']
    _require(isinstance(anchor, int) and anchor > 0, 'anchor block')

    holders_doc = docs['holders.json'].get(stpros)
    _require(holders_doc is not None, 'no StPROS holder inventory')
    _require(holders_doc['status'] == 'PASS' and holders_doc['mismatch_count'] == 0, 'holder inventory not clean')
    holders = sorted((_addr(a, 'holder'), _uint(b, f'balance {a}')) for a, b in holders_doc['holders'].items())
    supply = _uint(holders_doc['totalSupply_at_pin'], 'totalSupply_at_pin')
    _require(sum(b for _, b in holders) == supply, 'Σ holder balances != recorded total supply')
    _require(len([1 for _, b in holders if b]) == holders_doc['holder_count'], 'holder count')

    w = docs['withdrawals.json']
    _require(w['status'] == 'PASS' and not w['mismatches'], 'withdrawal inventory not clean')
    _require(_addr(w['stpros'], 'withdrawals.stpros') == stpros, 'withdrawal inventory is for another StPROS')
    rel = w['relation']
    queued = _uint(rel['Q_queuedWithdrawal'], 'Q')
    completed = _uint(rel['C_completedWithdrawal'], 'C')
    reserve = _uint(rel['R_totalCanWithdrawAmount'], 'R')
    _require(queued >= completed, 'Q < C')
    records = []
    for owner in sorted(w['records']):
        entry = w['records'][owner]
        _require(entry['tail'] - entry['head'] == len(entry['records']), f'{owner}: head/tail vs records')
        for i, r in enumerate(entry['records']):
            where = f'{owner}[{i}]'
            records.append({
                'owner': _addr(owner, where),
                'queued': _uint(r['queued'], f'{where}.queued'),
                'pending': _uint(r['pending'], f'{where}.pending'),
                'createdAt': _uint(r['createdAt'], f'{where}.createdAt'),
                'unbondingPeriod': _uint(r['unbondingPeriod'], f'{where}.unbondingPeriod'),
                # V1 records predate the receiver field; it is zero after the R4 migration.
                'receiver': _addr(r.get('receiver', ZERO_ADDRESS), f'{where}.receiver'),
            })
    sum_pending = sum(r['pending'] for r in records)
    _require(all(r['pending'] > 0 for r in records), 'zero pending record')
    _require(sum_pending == queued - completed, 'Σ pending != Q − C (I-4)')
    _require(sum_pending == _uint(rel['sum_pending'], 'sum_pending'), 'recomputed Σ pending differs from the record')
    _require(len({r['owner'] for r in records}) == w['owners_with_outstanding_records'], 'outstanding owner count')
    if chain == 'mainnet':
        # V1 design: the reserve backs exactly the outstanding records (PRE-4).
        _require(reserve == queued - completed, 'mainnet R != Q − C')

    return {
        'schema': SCHEMA,
        'chain': chain,
        'chainId': CHAINS[chain],
        'anchorBlock': anchor,
        'anchorBlockHash': summary['pinned_block_hash'],
        'stpros': {
            'address': stpros,
            'totalSupply': str(supply),
            'queued': str(queued),
            'completed': str(completed),
            'reserve': str(reserve),
            'holders': [a for a, _ in holders],
            'balances': [str(b) for _, b in holders],
            'recordOwners': [r['owner'] for r in records],
            'recordQueued': [str(r['queued']) for r in records],
            'recordPending': [str(r['pending']) for r in records],
            'recordCreatedAt': [str(r['createdAt']) for r in records],
            'recordUnbonding': [str(r['unbondingPeriod']) for r in records],
            'recordReceivers': [r['receiver'] for r in records],
        },
        'roles': _roles(docs['roles.json']),
        'owners': _owners(docs['owners.json']),
    }


def _roles(doc):
    rows = []
    for target in sorted(doc):
        entry = doc[target]
        _require(not entry['mismatches'], f'{target}: role mismatches')
        for role in sorted(entry['holders_at_pin']):
            for account in sorted(entry['holders_at_pin'][role]):
                rows.append({'target': _addr(target, 'role target'), 'role': role, 'account': _addr(account, 'role holder')})
    return rows


def _owners(doc):
    rows = []
    for target in sorted(doc):
        entry = doc[target]
        _require(entry['status'] == 'PASS' and not entry['mismatches'], f'{target}: ownership history not clean')
        rows.append({'target': _addr(target, 'owned target'), 'owner': _addr(entry['owner_at_pin'], 'owner')})
    return rows


def canonical(doc):
    return (json.dumps(doc, indent=2, sort_keys=True) + '\n').encode()


def write(doc, path):
    """Writes the canonical normalized inventory and returns its SHA-256 ('0x…')."""
    data = canonical(doc)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return '0x' + hashlib.sha256(data).hexdigest()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Normalize an explicitly supplied B0-format inventory.')
    parser.add_argument('--chain', required=True, choices=sorted(CHAINS))
    parser.add_argument('--stpros', required=True, help='expected StPROS proxy address')
    parser.add_argument('--dir', required=True, help='inventory directory (explicit; never discovered)')
    parser.add_argument('--expect-json', required=True, help='JSON file mapping each of FILES to its SHA-256')
    parser.add_argument('--out', required=True, help='output path inside the rehearsal cache')
    args = parser.parse_args()
    for path in (args.dir, args.expect_json, args.out):
        _require(not any(p.lower().startswith('.env') for p in Path(path).parts), 'refusing an environment path')
    _require(Path(args.out).resolve().is_relative_to(Path.cwd().resolve() / 'cache_forge/rehearsal'),
             'output must stay inside the export rehearsal cache')
    expected = json.loads(Path(args.expect_json).read_text())
    doc = normalize(read_authenticated(args.dir, expected), args.chain, args.stpros)
    print(json.dumps({'out': args.out, 'sha256': write(doc, args.out), 'anchorBlock': doc['anchorBlock'],
                      'holders': len(doc['stpros']['holders']), 'records': len(doc['stpros']['recordOwners'])}))


if __name__ == '__main__':
    main()
