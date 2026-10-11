"""Extract Solidity witness bytes from the frozen original Node output only."""
import argparse
import gzip
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAIRS = {
    'mainA': ('release-tooling.test.mjs:0001', 'release-tooling.test.mjs:0002'),
    'mainB': ('release-tooling.test.mjs:0018', 'release-tooling.test.mjs:0019'),
    'mainC': ('release-tooling.test.mjs:0023', 'release-tooling.test.mjs:0024'),
    'mainD': ('release-tooling.test.mjs:0028', 'release-tooling.test.mjs:0029'),
    'testnetNormal': (None, 'reference-supplement.mjs:0009'),
    'testnetE2': (None, 'reference-supplement.mjs:0010'),
    'testnetT3': (None, 'reference-supplement.mjs:0011'),
    'testnetCombined': (None, 'reference-supplement.mjs:0012'),
    'testnetPartial': (None, 'reference-supplement.mjs:0013'),
}


def extract():
    wanted = {case for pair in PAIRS.values() for case in pair if case}
    cases = {}
    for name in {case.split(':')[0] for case in wanted}:
        source = json.loads(gzip.decompress((HERE/'corpus'/(name+'.json.gz')).read_bytes()))
        cases.update({r['id']: r for r in source['records'] if r['id'] in wanted})
    index = {r['id']: r for r in json.loads((HERE/'corpus/index.json').read_text())}
    result = {}
    for name, (u_id, h_id) in PAIRS.items():
        h = cases[h_id]
        assert h['outcome'] == 'return'
        config = h['arguments'][0]
        if u_id:
            u = cases[u_id]
            assert u['outcome'] == 'return'
            execute = u['value']['execute']['data']
            operation_id = u['value']['operation']['id']
            handoff = h['value']['transactions'][-1]['data']
        else:
            stages = h['value']['stages']
            execute = stages[-1]['transactions'][0]['data']
            handoff = stages[-2]['assertion']['data']
            operation_id = '0x'+handoff[74:138]
        recovery = config.get('testnetRecovery', {})
        result[name] = {
            'sourceCases': [case for case in (u_id, h_id) if case],
            'sourceCaseSha256': [index[case]['sha256'] for case in (u_id, h_id) if case],
            'execute': execute, 'operationId': operation_id, 'handoff': handoff,
            'chainId': config['chainId'], 'addresses': config['addresses'],
            'actors': config['actors'], 'implementations': config['implementations'],
            'minDelay': config['policy']['minDelay'],
            'recovery': bool(recovery.get('e2Unavailable') or recovery.get('incompleteT3')),
            'allRetired': bool(recovery.get('e2Unavailable')),
        }
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', type=Path, required=True)
    args = parser.parse_args()
    if json.loads(args.check.read_text()) != extract():
        raise SystemExit('Solidity witness differs from original frozen output')
    print('PASS: nine original-engine Solidity witness input bundles')
