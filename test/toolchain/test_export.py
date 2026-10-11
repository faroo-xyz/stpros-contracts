"""Export boundary controls using owned temporary files only."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('native_export',ROOT/'tools/verify/export.py')
exporter=importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class ExportTests(unittest.TestCase):
    def test_invalid_paths_refused_without_access(self):
        for path in ['../outside.sol','contracts/../A.sol','/absolute.sol','contracts//A.sol','a\nb.sol']:
            with self.subTest(path=path),self.assertRaises(ValueError):exporter.safe_relative(path)

    def test_symlink_refused_before_any_source_read(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            base=Path(temp);source=base/'source';source.mkdir();(source/'contracts').mkdir()
            (source/'contracts/A.sol').symlink_to(base/'outside.sol')
            with patch.object(exporter,'ROOT',source),patch.object(exporter.subprocess,'check_output',return_value=b'contracts/A.sol\0'),patch.object(exporter.shutil,'copyfile',side_effect=AssertionError('copy started before validation')):
                with self.assertRaisesRegex(ValueError,'Symlink'):exporter.export(base/'export')
            self.assertFalse((base/'export').exists())

    def test_complete_list_validated_before_copy(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            base=Path(temp);source=base/'source';source.mkdir();(source/'contracts').mkdir()
            (source/'contracts/A.sol').write_text('contract A {}')
            (source/'contracts/Z.sol').mkdir()
            with patch.object(exporter,'ROOT',source),patch.object(exporter.subprocess,'check_output',return_value=b'contracts/A.sol\0contracts/Z.sol\0'),patch.object(exporter.shutil,'copyfile',side_effect=AssertionError('partial copy')):
                with self.assertRaisesRegex(ValueError,'Nonregular'):exporter.export(base/'export')
            self.assertFalse((base/'export').exists())

    def test_only_known_native_files_are_copied_and_hashed(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            base=Path(temp);source=base/'source';source.mkdir();(source/'contracts').mkdir()
            (source/'contracts/A.sol').write_text('contract A {}')
            (source/'local-not-exported.txt').write_text('owned synthetic data')
            with patch.object(exporter,'ROOT',source),patch.object(exporter.subprocess,'check_output',side_effect=[b'contracts/A.sol\0','a'*40+'\n']),redirect_stdout(io.StringIO()):exporter.export(base/'export')
            manifest=json.loads((base/'export/EXPORT-MANIFEST.txt').read_text())
            self.assertEqual(set(manifest['files']),{'contracts/A.sol'})
            self.assertFalse((base/'export/local-not-exported.txt').exists())

    def test_existing_and_live_destinations_refused(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp') as temp:
            base=Path(temp)
            with patch.object(exporter,'ROOT',base):
                for dest in (base,base/'nested'):
                    with self.assertRaises(ValueError):exporter.isolated_destination(dest)


if __name__=='__main__':unittest.main()
