"""Preserved Lua closures must retain shared captures and their source bodies."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

from luraph import cli

CORE = cli.VENDOR / 'core'
sys.path.insert(0, str(CORE))
from obfuscators.luraph_v15 import devirt as D

PARSER = cli.VENDOR / 'bin' / ('luau-ast.exe' if os.name == 'nt' else 'luau-ast')
RUNTIME = cli.VENDOR / 'bin' / ('luau.exe' if os.name == 'nt' else 'luau')


@unittest.skipUnless(PARSER.is_file() and RUNTIME.is_file(), 'build Luau first')
class PlainClosureTests(unittest.TestCase):
    def recover(self, expression, captured):
        source = 'local captures\nreturn ' + expression
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.lua'
            path.write_bytes(source.encode('latin1'))
            root = D.vmmap.load_ast(str(path))
        node = root['body'][1]['list'][0]
        scope = D.Scope()
        scope.vars[root['body'][0]['vars'][0]['location']] = captured
        lf = D.ProtoLifter.__new__(D.ProtoLifter)
        lf.vm = SimpleNamespace(src_lines=source.split('\n'))
        lf.captured_regs = set()
        lf.children = []
        closure = lf.as_expr(D.LuaFunc(node, scope))
        return lf, closure, source

    def render(self, closure, names=None, parent_names=None):
        closure.capnames = names or {7: 'state', 8: 'other'}
        lifter = D.FunctionLifter.__new__(D.FunctionLifter)
        return '\n'.join(lifter.closure(None, None, closure, parent_names or {}, 0).lines)

    def execute(self, source):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.lua'
            path.write_text(source)
            return subprocess.check_output([str(RUNTIME), str(path)], timeout=10)

    def test_complete_v15_pipeline_preserves_shared_mutations(self):
        fixture = Path(__file__).parent / 'fixtures' / 'vm-plain-closure.lua'
        expected = self.execute(fixture.read_text())
        self.assertEqual(expected, b'23\n42\n42\n')
        with tempfile.TemporaryDirectory() as tmp:
            for version in ('15.0',):
                source = Path(tmp) / ('source-' + version + '.lua')
                source.write_text(fixture.read_text().replace('v15.0', 'v' + version))
                output = Path(tmp) / ('output-' + version + '.lua')
                self.assertEqual(cli.main([str(source), '-o', str(output), '--rounds', '5']), 0)
                self.assertEqual(self.execute(output.read_text()), expected)

    def test_boxed_reads_writes_and_callbacks_share_parent_local(self):
        expression = '''function(value)
    if value ~= nil then captures[1][5][captures[1][4]] = value end
    return captures[1][5][captures[1][4]]
end'''
        captures = D.LTable()
        captures.set(1, D.MaybeBox(7))
        lf, closure, _ = self.recover(expression, captures)
        self.assertIsInstance(closure.proto, D.PlainProto)
        self.assertEqual(lf.captured_regs, {7})
        recovered = self.render(closure)
        original = 'local frame = {11}; local captures = {{[5]=frame, [4]=1}}\nlocal f = ' + expression + '\n'
        transformed = 'local state = 11\nlocal f = ' + recovered + '\n'
        calls = 'print(f()); print(f(23)); print(f())\n'
        self.assertEqual(self.execute(original + calls), self.execute(transformed + calls))
        self.assertEqual(self.execute(transformed + calls + 'state=42; print(f())'), b'11\n23\n23\n42\n')

    def test_two_boxes_remain_distinct_and_nested_closure_captures_them(self):
        expression = '''function()
    return function() captures[1][5][captures[1][4]] += 1
        return captures[2][5][captures[2][4]], captures[1][5][captures[1][4]] end
end'''
        captures = D.LTable()
        captures.set(1, D.MaybeBox(7))
        captures.set(2, D.MaybeBox(8))
        lf, closure, _ = self.recover(expression, captures)
        self.assertEqual(lf.captured_regs, {7, 8})
        code = 'local state, other = 10, 30\nlocal factory=' + self.render(closure)
        self.assertEqual(self.execute(code + '\nlocal f=factory(); print(f()); other=40; print(f())'), b'30\t11\n40\t12\n')

    def test_multiline_byte_columns_and_direct_register_capture(self):
        captures = D.LTable()
        captures.set(1, D.Reg(7))
        _, closure, _ = self.recover('function()\n local text="caf\xe9"\n return text, captures[1]\nend', captures)
        code = 'local state=42\nlocal f=' + self.render(closure) + '\nprint(f())'
        self.assertEqual(self.execute(code), b'caf\xe9\t42\n')

    def test_box_from_parent_upvalue_uses_parent_binding(self):
        captures = D.LTable()
        captures.set(1, D.BoxProxy(2))
        _, closure, _ = self.recover('function() return captures[1][5][captures[1][4]] end', captures)
        code = 'local shared=42\nlocal f=' + self.render(closure, parent_names={('up', 2): 'shared'}) + '\nprint(f())'
        self.assertEqual(self.execute(code), b'42\n')

    def test_utf8_string_bytes_are_not_encoded_twice(self):
        value = 'café2'.encode().decode('latin1')
        _, recovered, _ = self.recover('function() return "' + value + '" end', D.LTable())
        self.assertEqual(self.execute('local f=' + recovered.src + '\nprint(f())'), 'café2\n'.encode())

    def test_large_uncaptured_body_is_preserved(self):
        expression = 'function() local text="' + 'x' * 2200 + '"; return #text end'
        _, recovered, _ = self.recover(expression, D.LTable())
        self.assertIsInstance(recovered, D.Opaque)
        self.assertEqual(self.execute('local f=' + recovered.src + '\nprint(f())'), b'2200\n')

    def test_unresolved_capture_is_partial_instead_of_empty_function(self):
        _, recovered, _ = self.recover('function() return captures[1] end', D.LTable())
        self.assertIsInstance(recovered, D.Opaque)
        self.assertIn('devirt: unresolved plain closure', recovered.src)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'partial.lua'
            path.write_text('return ' + recovered.src)
            self.assertEqual(cli.validate_output(path, 'lifted', PARSER, 10)['kind'], 'partial')

    def test_unresolved_opaque_function_is_partial(self):
        source = 'function() return captures[1] end'
        lf, _, _ = self.recover(source, D.LTable())
        opaque = D.OpaqueFn(1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.lua'
            path.write_text('local captures\nreturn ' + source)
            opaque.node = D.vmmap.load_ast(str(path))['body'][1]['list'][0]
        value = lf.as_expr(opaque)
        self.assertIn('devirt: unresolved plain closure', value.src)
