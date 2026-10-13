"""Focused native compiler/reference controls; no EVM, RPC or Hardhat."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/ci'))
import canonical as native


class NativeBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lock = json.loads(native.LOCK.read_text())
        cls.resolver = native.snapshot_build.Resolver()
        cls.solc = native.snapshot_build.find_solc()
        native.compiler_identity(cls.solc, cls.lock)
        cls.ref = cls.lock['contracts']['YieldVaultFactory']
        cls.root = cls.ref['source']
        cls.std = native.snapshot_build.standard_input(cls.root, 'production', cls.resolver, native.EXTRA)
        cls.output = native.snapshot_build.compile_input(cls.std, cls.solc)
        cls.artifact = native.artifact(cls.root, 'YieldVaultFactory', cls.std, cls.output)

    def test_original_artifact_hashes_are_unchanged(self):
        encoded = json.dumps(self.lock['contracts'], sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(native.sha(encoded), '23b97c06c771f86118292add069d3ccd8c317852034029ff32f9c813a26025c5')

    def test_configuration_and_vendor_drift_refused_before_build(self):
        native.configuration(self.lock)
        original = Path.read_bytes
        for target in ['foundry.toml', 'lib/openzeppelin-contracts/proxy/transparent/ProxyAdmin.sol']:
            def changed(path):
                return b'changed' if path == native.ROOT / target else original(path)
            with self.subTest(target=target), patch.object(Path, 'read_bytes', changed):
                with self.assertRaisesRegex(ValueError, 'drift'):
                    native.configuration(self.lock)

    def test_positive_exact_compiler_input_and_artifact(self):
        native.dependencies(self.resolver, self.lock)
        native.validate_input(self.std, self.root, self.ref, self.lock)
        native.compare(self.artifact, self.ref)

    def test_wrong_compiler_bytes_rejected_before_execution(self):
        lock = dict(self.lock, compilerSha256='0' * 64)
        with patch.object(native.subprocess, 'check_output') as execute:
            with self.assertRaisesRegex(ValueError, 'compiler binary'):
                native.compiler_identity(self.solc, lock)
            execute.assert_not_called()

    def test_wrong_compiler_version(self):
        with self.assertRaisesRegex(ValueError, 'version'):
            native.compiler_identity(self.solc, dict(self.lock, compiler='0.8.29'))

    def test_unreviewed_dependency(self):
        with patch.object(self.resolver, 'oz', '5.6.2'):
            with self.assertRaisesRegex(ValueError, 'dependency'):
                native.dependencies(self.resolver, self.lock)

    def test_settings_fail_closed(self):
        for key, value in [('evmVersion', 'paris'), ('optimizer', {'enabled': True, 'runs': 201}),
                           ('viaIR', True), ('metadata', {'useLiteralContent': True, 'bytecodeHash': 'none'}),
                           ('remappings', [])]:
            with self.subTest(key=key):
                std = copy.deepcopy(self.std); std['settings'][key] = value
                with self.assertRaisesRegex(ValueError, 'settings'):
                    native.validate_input(std, self.root, self.ref, self.lock)

    def test_renamed_source_and_source_change(self):
        for rename in [False, True]:
            std = copy.deepcopy(self.std)
            if rename:
                std['sources'][self.root.replace('project/', '')] = std['sources'].pop(self.root)
            else:
                std['sources'][self.root]['content'] += '\n// drift\n'
            with self.assertRaisesRegex(ValueError, 'source closure'):
                native.validate_input(std, self.root, self.ref, self.lock)

    def test_artifact_mismatch_controls(self):
        for key in ['abi', 'selectors', 'storageLayout', 'namespaces', 'creationBytecode',
                    'deployedBytecode', 'runtimeKeccak', 'immutableReferences', 'metadata']:
            with self.subTest(field=key):
                bad = copy.deepcopy(self.artifact); bad[key] = 'wrong'
                with self.assertRaisesRegex(ValueError, key):
                    native.compare(bad, self.ref)

    def test_actual_changed_optimizer_runtime_is_rejected(self):
        std = copy.deepcopy(self.std); std['settings']['optimizer']['runs'] = 201
        out = native.snapshot_build.compile_input(std, self.solc)
        bad = native.artifact(self.root, 'YieldVaultFactory', std, out)
        with self.assertRaises(ValueError):
            native.compare(bad, self.ref)
        self.assertNotEqual(bad['deployedBytecode'], self.artifact['deployedBytecode'])

    def test_actual_metadata_none_is_rejected_without_stripping(self):
        std = copy.deepcopy(self.std); std['settings']['metadata']['bytecodeHash'] = 'none'
        out = native.snapshot_build.compile_input(std, self.solc)
        bad = native.artifact(self.root, 'YieldVaultFactory', std, out)
        with self.assertRaises(ValueError):
            native.compare(bad, self.ref)
        self.assertNotEqual(bad['deployedBytecode'], self.artifact['deployedBytecode'])

    def test_immutable_binding_every_occurrence_and_wrong_address(self):
        refs = self.artifact['immutableReferences']
        self.assertEqual(len(refs), 1)
        ident = next(iter(refs)); address = '0x' + '12' * 20
        bound = native.bind_runtime(self.artifact, {ident: address})
        # Independent offset reconstruction, not the binder as its own oracle.
        expected = bytes.fromhex(self.artifact['deployedBytecode'][2:])
        for slot in refs[ident]:
            i = slot['start']; expected = expected[:i] + bytes(12) + bytes.fromhex(address[2:]) + expected[i+32:]
        self.assertEqual(bound, '0x' + expected.hex())
        native.verify_bound_runtime(self.artifact, bound, {ident: address})
        with self.assertRaisesRegex(ValueError, 'bound runtime mismatch'):
            native.verify_bound_runtime(self.artifact, bound, {ident: '0x' + '34' * 20})
        for bindings in [{}, {'wrong': address}, {ident: '0x' + '00' * 20}]:
            with self.assertRaises(ValueError):
                native.bind_runtime(self.artifact, bindings)
        bad = copy.deepcopy(self.artifact); bad['immutableReferences'][ident][0]['start'] = 10**9
        with self.assertRaisesRegex(ValueError, 'range'):
            native.bind_runtime(bad, {ident: address})

    def test_missing_import_cannot_fall_back_to_filesystem(self):
        std = copy.deepcopy(self.std)
        key = next(k for k in std['sources'] if k != self.root)
        del std['sources'][key]
        with self.assertRaises(RuntimeError):
            native.snapshot_build.compile_input(std, self.solc)

    def test_source_path_escape(self):
        for key in ['project/contracts/../../README.sol', 'project/tasks/source.sol', 'project/contracts/a.txt']:
            with self.assertRaises((ValueError, KeyError)):
                self.resolver.key_to_path(key)


if __name__ == '__main__':
    unittest.main()
