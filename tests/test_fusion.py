import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from luraph import cli

BIN = cli.VENDOR / 'bin'
PARSER = BIN / ('luau-ast.exe' if os.name == 'nt' else 'luau-ast')
RUNTIME = BIN / ('luau.exe' if os.name == 'nt' else 'luau')


class DetectionTests(unittest.TestCase):
    def test_versions_and_unknown_banners(self):
        for version in cli.VERSIONS:
            self.assertEqual(cli.detect(f'-- This file was protected using Luraph Obfuscator v{version}')['engine'], version)
        for version in ('13.9', '14.6', '16.0'):
            self.assertIsNone(cli.detect(f'-- This file was protected using Luraph Obfuscator v{version}')['engine'])

    def test_headerless_detection_does_not_guess_v14_minor(self):
        self.assertIsNone(cli.detect('return(function() loadstring("x") end)()')['engine'])
        self.assertEqual(cli.detect('return setmetatable({[1] = buffer.fromstring})')['engine'], '15')
        self.assertIsNone(cli.detect('print("hello")')['engine'])

    def test_invalid_limits(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with self.assertRaises(argparse.ArgumentTypeError):
                cli.positive(value)

    def test_header_repair_does_not_modify_embedded_comments(self):
        js = cli.VENDOR / 'src' / 'detect.js'
        command = ["node", '-e', '''
const assert = require('node:assert/strict');
const {restoreHeaderNewline: fix} = require(process.argv[1]);
const h = '-- This file was protected using Luraph Obfuscator v15.0 [https://lura.ph/]';
assert.equal(fix(h), h);
assert.equal(fix(h + 'print(1)'), h + '\\nprint(1)');
assert.equal(fix('print(1)\\n' + h + 'print(2)'), 'print(1)\\n' + h + 'print(2)');
''', str(js)]
        subprocess.run(command, check=True)


class SelectionTests(unittest.TestCase):
    def test_structural_result_beats_larger_trace(self):
        args = argparse.Namespace(allow_partial=True, allow_trace=True, trace_only=False)
        candidates = [{'backend': 'a', 'kind': 'trace', 'top_level_statements': 10000},
                      {'backend': 'b', 'kind': 'partial', 'unresolved_markers': 2},
                      {'backend': 'c', 'kind': 'partial', 'unresolved_markers': 1}]
        self.assertEqual(cli.choose(candidates, args)['backend'], 'c')

    def test_default_rejects_partial_and_trace(self):
        args = argparse.Namespace(allow_partial=False, allow_trace=False, trace_only=False)
        self.assertIsNone(cli.choose([{'kind': 'trace'}, {'kind': 'partial'}, {'kind': 'unverified'}], args))
        self.assertEqual(cli.choose([{'kind': 'lifted'}], args)['kind'], 'lifted')

    def test_invalid_never_selected(self):
        args = argparse.Namespace(allow_partial=True, allow_trace=True, trace_only=True)
        self.assertIsNone(cli.choose([{'kind': 'invalid'}, {'kind': 'failed'}], args))


class ProcessTests(unittest.TestCase):
    def test_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            cli.run_process([sys.executable, '-c', 'import time; time.sleep(20)'], 0.1)

    @unittest.skipIf(os.name == 'nt', 'POSIX process groups')
    def test_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'child-finished'
            script = 'import time; from pathlib import Path; time.sleep(1); Path(' + repr(str(marker)) + ').touch()'
            parent = 'import subprocess,time,sys; subprocess.Popen([sys.executable,"-c",' + repr(script) + ']); time.sleep(20)'
            with self.assertRaises(subprocess.TimeoutExpired):
                cli.run_process([sys.executable, '-c', parent], 0.3)
            import time
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_failure_preserves_output_and_records_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            input_file, output = Path(temp) / 'in.lua', Path(temp) / 'out.luau'
            input_file.write_text('-- This file was protected using Luraph Obfuscator v15.0')
            output.write_text('previous result')
            args = argparse.Namespace(engine=None, backend='caomod2077', allow_partial=False,
                                      allow_trace=False, trace_only=False)
            with patch.object(cli, 'run_backend', return_value=({'backend': 'caomod2077', 'kind': 'trace'}, output)):
                with self.assertRaisesRegex(RuntimeError, 'No acceptable'):
                    cli.recover(input_file, output, args)
            self.assertEqual(output.read_text(), 'previous result')
            self.assertIsNone(json.loads(Path(str(output) + '.json').read_text())['selected'])

    def test_overwriting_input_and_batch_collisions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            a, b = directory / 'same.lua', directory / 'same.luau'
            a.write_text('print(1)')
            b.write_text('print(2)')
            with patch.object(Path, 'is_file', return_value=True), patch.object(cli, 'recover') as recover:
                self.assertEqual(cli.main([str(a), '-o', str(a)]), 1)
                self.assertEqual(cli.main([str(directory), '-o', str(directory / 'out')]), 1)
                recover.assert_not_called()


@unittest.skipUnless(PARSER.is_file() and RUNTIME.is_file(), 'build Luau runtime first')
class RuntimeTests(unittest.TestCase):
    def test_validator_rejects_invalid_and_comment_only_output(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'out.lua'
            for text in ('local = ?', '-- only comments'):
                output.write_text(text)
                self.assertEqual(cli.validate_output(output, 'lifted', PARSER, 10)['kind'], 'invalid')
            output.write_text('error("devirt: missing block")')
            self.assertEqual(cli.validate_output(output, 'lifted', PARSER, 10)['kind'], 'partial')

    def test_trace_is_identified_and_syntax_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'in.lua', Path(temp) / 'out.lua'
            source.write_text('-- This file was protected using Luraph Obfuscator v15.0\nprint("fusion")\n')
            self.assertEqual(cli.main([str(source), '-o', str(output), '--trace-only', '--wall-timeout', '30']), 0)
            report = json.loads(Path(str(output) + '.json').read_text())
            self.assertEqual(report['selected']['kind'], 'trace')
            self.assertEqual(report['selected']['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
            result = subprocess.check_output([str(RUNTIME), str(output)])
            self.assertIn(b'fusion', result)

    def test_real_lifting_matches_synthetic_vm_behaviour(self):
        source = Path(__file__).parent / 'fixtures' / 'vm.lua'
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'lifted.luau'
            self.assertEqual(cli.main([str(source), '-o', str(output), '--wall-timeout', '60', '--rounds', '5']), 0)
            report = json.loads(Path(str(output) + '.json').read_text())
            self.assertEqual(report['selected']['kind'], 'lifted')
            self.assertNotIn('while true', output.read_text())
            original = subprocess.check_output([str(RUNTIME), str(source)])
            recovered = subprocess.check_output([str(RUNTIME), str(output)])
            self.assertEqual(original, recovered)
            self.assertEqual(recovered.strip(), b'42')

    def test_lifting_preserves_both_parameter_dependent_branches(self):
        source = Path(__file__).parent / 'fixtures' / 'vm-branch.lua'
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            output = directory / 'lifted.lua'
            self.assertEqual(cli.main([str(source), '-o', str(output), '--rounds', '5']), 0)
            for value in (1, -1):
                original = directory / 'original.lua'
                original.write_text(source.read_text().replace('main(1)', f'main({value})'))
                recovered = directory / 'recovered.lua'
                recovered.write_text('(function(...)\n' + output.read_text() + f'\nend)({value})')
                self.assertEqual(subprocess.check_output([str(RUNTIME), str(original)]),
                                 subprocess.check_output([str(RUNTIME), str(recovered)]))

    def test_v14_and_v15_jobs_do_not_share_engine_state(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fixture = (Path(__file__).parent / 'fixtures' / 'vm.lua').read_text()
            for version in ('14.7', '15.0'):
                source = directory / (version + '.lua')
                output = directory / (version + '.out.lua')
                source.write_text(fixture.replace('v15.0', 'v' + version))
                self.assertEqual(cli.main([str(source), '-o', str(output), '--rounds', '5']), 0)
                self.assertEqual(subprocess.check_output([str(RUNTIME), str(output)]).strip(), b'42')

    def test_mutable_variadic_frame_is_not_accepted_as_success(self):
        source = Path(__file__).parent / 'fixtures' / 'vm-varargs.lua'
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'out.luau'
            self.assertEqual(cli.main([str(source), '-o', str(output), '--rounds', '5']), 1)
            report = json.loads(Path(str(output) + '.json').read_text())
            self.assertIsNone(report['selected'])
            self.assertEqual(report['candidates'][0]['kind'], 'partial')
            self.assertFalse(output.exists())

    def test_variadic_tail_writes_override_original_values(self):
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from obfuscators.luraph_v15.devirt import ProtoLifter
from luasym import SymList, VarargTail
# Exercise the engine's reader on a list after an explicit tail-slot store.
reader = ProtoLifter.__new__(ProtoLifter)
values = SymList([], VarargTail(1))
values.extra[2] = 42
values.extra[3] = None
assert reader.symlist_get(values, 2) == 42
assert reader.symlist_get(values, 3) is None
"""
        subprocess.run([sys.executable, '-c', code, str(cli.VENDOR / 'core')], check=True)

    def test_external_adapter_with_cli_double(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            entry = directory / 'Deobfuscator' / 'deobf'
            entry.mkdir(parents=True)
            (entry / 'cli.py').write_text('import sys; from pathlib import Path; Path(sys.argv[sys.argv.index("-o")+1]).write_text("print(42)")')
            source, output = directory / 'in.lua', directory / 'out.lua'
            source.write_text('-- This file was protected using Luraph Obfuscator v14.7')
            self.assertEqual(cli.main([str(source), '-o', str(output), '--backend', 'kryptit',
                                      '--kryptit-root', str(directory), '--allow-partial']), 0)
            report = json.loads(Path(str(output) + '.json').read_text())
            self.assertEqual(report['selected']['kind'], 'unverified')


if __name__ == '__main__':
    unittest.main()
