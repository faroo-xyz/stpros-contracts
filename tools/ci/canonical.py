#!/usr/bin/env python3
"""Pinned R4 native build. Run inside the approved export; never refresh B0.

The reference is a reviewed-input gate, not release authorization. All artifact
fields are compared exactly, including IPFS metadata and AST-based storage IDs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'tools/snapshot'), str(ROOT / 'tools/lib')]
import build as snapshot_build
import storage
import common
from keccak import keccak256

LOCK = ROOT / 'tools/ci/r4-reference.json'
EXTRA = ('metadata', 'evm.deployedBytecode.immutableReferences',
         'evm.bytecode.linkReferences', 'evm.deployedBytecode.linkReferences')


def canonical(obj):
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def compiler_identity(solc, lock):
    # Verify bytes BEFORE execution; an arbitrary program claiming the version is insufficient.
    require(sha(Path(solc).read_bytes()) == lock['compilerSha256'], 'unreviewed compiler binary')
    version = subprocess.check_output([solc, '--version'], cwd=ROOT, env={}, text=True)
    require(version == lock['compiler'], 'incorrect compiler version')
    return version


def dependencies(resolver, lock):
    require({'contracts': resolver.oz, 'contracts-upgradeable': resolver.ozu} == lock['dependencies'],
            'unreviewed dependency version')


def configuration(lock):
    for path, digest in lock['configSha256'].items():
        common.refuse_secret_path(path)
        require(sha((ROOT / path).read_bytes()) == digest, f'configuration drift: {path}')
    vendor = json.loads((ROOT / 'lib/DEPENDENCIES.json').read_text())
    for path, digest in vendor['files'].items():
        common.refuse_secret_path(path)
        require(path.startswith('lib/') and '..' not in Path(path).parts, 'invalid vendor path')
        source = ROOT / path
        require(not source.is_symlink() and source.resolve().is_relative_to((ROOT/'lib').resolve()), 'vendor escape')
        require(sha(source.read_bytes()) == digest, f'vendored source drift: {path}')


def validate_input(std, root, reference, lock):
    require(std['language'] == 'Solidity', 'compiler language drift')
    settings = {k: v for k, v in std['settings'].items() if k != 'outputSelection'}
    require(settings == lock['settings'], 'compiler settings / metadata mismatch')
    require(root == reference['source'], 'source identity mismatch')
    sources = {k: sha(v['content'].encode()) for k, v in std['sources'].items()}
    require(sources == reference['sourceClosureSha256'], 'source closure / identity mismatch')


def artifact(root, name, std, output):
    c = output['contracts'][root][name]
    idx = storage.ast_index({k: v['ast'] for k, v in output['sources'].items()})
    node = next(n for n, k in idx.values() if k == root and n['name'] == name)
    texts = {k: v['content'] for k, v in std['sources'].items()}
    runtime = '0x' + c['evm']['deployedBytecode']['object']
    return {
        'source': root, 'abi': c['abi'], 'selectors': c['evm']['methodIdentifiers'],
        'storageLayout': c['storageLayout'], 'namespaces': storage.namespaces(node, idx, texts),
        'creationBytecode': '0x' + c['evm']['bytecode']['object'], 'deployedBytecode': runtime,
        'immutableReferences': c['evm']['deployedBytecode']['immutableReferences'],
        'creationLinks': c['evm']['bytecode']['linkReferences'],
        'runtimeLinks': c['evm']['deployedBytecode']['linkReferences'],
        'metadata': json.loads(c['metadata']), 'runtimeKeccak': keccak256(bytes.fromhex(runtime[2:])),
        'sourceClosureSha256': {k: sha(v.encode()) for k, v in sorted(texts.items())},
    }


def compare(actual, reference):
    require(set(actual) == set(reference['fieldSha256']), 'artifact fields mismatch')
    for field, digest in reference['fieldSha256'].items():
        require(sha(canonical(actual[field])) == digest, f'{field} mismatch')


def bind_runtime(artifact, bindings):
    """Exact, complete immutable-ID binding. No address inference or metadata stripping."""
    refs = artifact['immutableReferences']
    require(set(bindings) == set(refs), 'incorrect immutable binding set')
    runtime = bytearray.fromhex(artifact['deployedBytecode'][2:])
    occupied = set()
    for ident, slots in refs.items():
        address = bindings[ident]
        require(re.fullmatch(r'0x[0-9a-f]{40}', address) is not None and int(address, 16) != 0,
                'incorrect immutable address binding')
        require(bool(slots), 'empty immutable references')
        word = bytes.fromhex(address[2:]).rjust(32, b'\0')
        for slot in slots:
            start, length = slot['start'], slot['length']
            require(length == 32 and start >= 0 and start + length <= len(runtime), 'invalid immutable range')
            positions = set(range(start, start + length))
            require(not occupied & positions and runtime[start:start + length] == bytes(length),
                    'overlapping or nonzero immutable template')
            occupied.update(positions)
            runtime[start:start + length] = word
    return '0x' + runtime.hex()


def verify_bound_runtime(artifact, observed, bindings):
    require(observed == bind_runtime(artifact, bindings), 'bound runtime mismatch')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--solc', help='Exact approved native binary; default: existing SVM resolver')
    args = ap.parse_args()
    require((ROOT / 'EXPORT-MANIFEST.txt').is_file(), 'run only in the approved isolated export')
    dest = ROOT / 'cache/native-r4'
    (dest / 'manifest.json').unlink(missing_ok=True)
    lock = json.loads(LOCK.read_text())
    configuration(lock)
    solc = args.solc or snapshot_build.find_solc()
    common.refuse_secret_path(solc)
    compiler_identity(solc, lock)
    resolver = snapshot_build.Resolver()
    dependencies(resolver, lock)
    require(snapshot_build.root_files() == lock['productionRoots'], 'production source inventory drift')
    jobs = {}
    # Validate the entire build before publishing any candidate artifact.
    for name, ref in lock['contracts'].items():
        root = ref['source']
        std = snapshot_build.standard_input(root, 'production', resolver, EXTRA)
        validate_input(std, root, ref, lock)
        out = snapshot_build.compile_input(std, solc)
        result = artifact(root, name, std, out)
        compare(result, ref)
        jobs[name] = (std, out, result)
    dest = ROOT / 'cache/native-r4'
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {'schema': 'faroo-native-r4-v1', 'referenceSha256': sha(LOCK.read_bytes()),
                'compilerSha256': lock['compilerSha256'], 'sourceCommit': lock['sourceCommit'],
                'sourceTree': lock['sourceTree'], 'pythonVersion': sys.version, 'contracts': {},
                'toolingSha256': {p: sha((ROOT / p).read_bytes()) for p in
                    ['tools/ci/canonical.py', 'tools/snapshot/build.py', 'tools/snapshot/storage.py', 'tools/lib/keccak.py', 'tools/lib/common.py']}}
    for name, (std, out, result) in jobs.items():
        directory = dest / name
        directory.mkdir(exist_ok=True)
        for filename, value in [('input.json', std), ('output.json', out), ('artifact.json', result)]:
            (directory / filename).write_bytes(canonical(value))
        # Existing release normalizer accepts this envelope; no release logic changes.
        info = {'solcVersion': '0.8.28', 'input': std, 'output': out}
        (directory / 'build-info.json').write_bytes(canonical(info))
        manifest['contracts'][name] = {'artifactSha256': sha(canonical(result)),
                                     'inputSha256': sha(canonical(std)), 'runtimeKeccak': result['runtimeKeccak'],
                                     'historicalCoverage': lock['contracts'][name]['historicalCoverage']}
        print(name + ': exact reference parity')
    (dest / 'manifest.json').write_bytes(canonical(manifest))
    print('PASS: native reference parity (historical coverage limits remain explicit)')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        sys.exit(str(error))
