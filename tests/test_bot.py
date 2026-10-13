import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import bot as application
import prepare_engine

ROOT = Path(application.__file__).resolve().parent
NATIVE_READY = (ROOT / 'vendor' / 'caomod2077' / 'bin' / ('luau.exe' if os.name == 'nt' else 'luau')).is_file()


class BotCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.busy = patch.object(application, 'BUSY', asyncio.Lock())
        self.busy.start()
        self.bot = application.create_bot(123)
        self.bot.engine.run = AsyncMock(return_value=b'print("prometheus")\n')
        self.bot.luraph_engine.run = AsyncMock(return_value=b'print(42)\n')
        self.messages = []
        self.uploads = []

    async def asyncTearDown(self):
        await self.bot.close()
        self.busy.stop()

    def context(self, filename='input.lua', content=b'protected source', owner=123):
        attachment = SimpleNamespace(filename=filename, size=len(content), read=AsyncMock(return_value=content))

        async def send(text, file=None):
            self.messages.append(text)
            if file:
                self.uploads.append((file.filename, file.fp.read()))

        return SimpleNamespace(author=SimpleNamespace(id=owner),
                               message=SimpleNamespace(attachments=[attachment]),
                               guild=SimpleNamespace(filesize_limit=10 * 1024 * 1024), send=send)

    async def invoke(self, ctx, url='', version=''):
        await self.bot.get_command('luraph').callback(ctx, url, version)

    async def test_attachment_delivers_lua_and_uses_cache(self):
        ctx = self.context(filename='input.luau')
        await self.invoke(ctx)
        await self.invoke(ctx)
        self.assertEqual(self.uploads, [('resultado.lua', b'print(42)\n')] * 2)
        self.bot.luraph_engine.run.assert_awaited_once()
        self.bot.engine.run.assert_not_awaited()
        self.assertIn('caché', self.messages[-1])

    @unittest.skipUnless(NATIVE_READY, 'build Luau runtime first')
    async def test_real_partial_recovery_is_delivered_with_warning(self):
        self.bot.luraph_engine = application.LuraphDeobfuscator(ROOT)
        source = (ROOT / 'tests' / 'fixtures' / 'vm-varargs.lua').read_bytes()
        await self.invoke(self.context(content=source))
        self.assertEqual(len(self.uploads), 1, self.messages)
        self.assertTrue(self.uploads[0][1].startswith(application.PARTIAL_HEADER))
        self.assertIn('recuperación parcial', self.messages[-1])

    async def test_cache_hit_does_not_prepare_missing_engine(self):
        ctx = self.context()
        self.bot.luraph_engine.ensure_ready = AsyncMock()
        await self.invoke(ctx)
        self.bot.luraph_engine.ensure_ready.reset_mock()
        self.bot.luraph_engine.ensure_ready.side_effect = application.ProcessingError('engine unavailable')
        self.bot.engine_errors['luraph'] = 'engine unavailable'
        await self.invoke(ctx)
        self.bot.luraph_engine.ensure_ready.assert_not_awaited()
        self.assertEqual(self.uploads, [('resultado.lua', b'print(42)\n')] * 2)

    async def test_version_is_forwarded_and_has_distinct_cache_entry(self):
        ctx = self.context()
        await self.invoke(ctx, '14.8')
        await self.invoke(ctx, '14.9')
        versions = [call.kwargs['engine'] for call in self.bot.luraph_engine.run.await_args_list]
        self.assertEqual(versions, ['14.8', '14.9'])

    async def test_prometheus_and_luraph_cache_are_separate(self):
        ctx = self.context()
        await self.bot.get_command('deobf').callback(ctx)
        await self.invoke(ctx)
        self.assertEqual([content for _, content in self.uploads], [b'print("prometheus")\n', b'print(42)\n'])
        self.bot.engine.run.assert_awaited_once()
        self.bot.luraph_engine.run.assert_awaited_once()

    async def test_partial_warning_survives_cache(self):
        self.bot.luraph_engine.run.return_value = application.PARTIAL_HEADER + b'print(42)\n'
        ctx = self.context()
        await self.invoke(ctx)
        await self.invoke(ctx)
        self.assertTrue(all('recuperación parcial' in message for message in self.messages))
        self.assertTrue(self.uploads[-1][1].startswith(application.PARTIAL_HEADER))

    async def test_owner_only_and_input_validation(self):
        await self.invoke(self.context(owner=124))
        self.assertEqual(self.messages, [])
        await self.invoke(self.context(filename='input.exe'))
        await self.invoke(self.context(content=b'x' * (application.MAX_INPUT + 1)))
        await self.invoke(self.context(), '', '16')
        self.bot.luraph_engine.run.assert_not_awaited()
        self.assertEqual(len(self.messages), 3)
        self.assertEqual(self.uploads, [])

    async def test_busy_failure_and_upload_limit_return_no_file(self):
        ctx = self.context()
        async with application.BUSY:
            await self.invoke(ctx)
        self.bot.luraph_engine.run.assert_not_awaited()
        self.bot.luraph_engine.run.side_effect = application.ProcessingError('No pude desvirtualizar')
        await self.invoke(ctx)
        self.assertIn('No pude desvirtualizar', self.messages[-1])
        self.bot.luraph_engine.run.side_effect = None
        ctx.guild.filesize_limit = 1
        await self.invoke(ctx)
        self.assertIn('límite', self.messages[-1])
        self.assertEqual(self.uploads, [])

    async def test_direct_url_delivers_file_without_attachment(self):
        ctx = self.context()
        ctx.message.attachments = []
        response = SimpleNamespace(status=200, content=SimpleNamespace())

        async def chunks(_):
            yield b'protected URL source'

        response.content.iter_chunked = chunks
        response_context = AsyncMock()
        response_context.__aenter__.return_value = response
        session = SimpleNamespace(get=lambda *args, **kwargs: response_context)
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        with patch.object(application.aiohttp, 'ClientSession', return_value=session_context):
            await self.invoke(ctx, 'https://example.com/input.lua')
        self.assertEqual(self.uploads, [('resultado.lua', b'print(42)\n')])
        self.bot.luraph_engine.run.assert_awaited_once()

    async def test_missing_prometheus_does_not_block_luraph_setup(self):
        self.bot.engine.prepare = AsyncMock(side_effect=application.ProcessingError('Prometheus unavailable'))
        self.bot.luraph_engine.prepare = AsyncMock()
        await self.bot.setup_hook()
        self.assertIn('deobf', self.bot.engine_errors)
        self.assertNotIn('luraph', self.bot.engine_errors)
        await self.invoke(self.context())
        self.assertEqual(len(self.uploads), 1)

    async def test_startup_failure_is_retried_after_engine_is_installed(self):
        self.bot.engine_errors['luraph'] = 'No encuentro Luraph'
        self.bot.luraph_engine.ensure_ready = AsyncMock()
        await self.invoke(self.context())
        self.bot.luraph_engine.ensure_ready.assert_awaited_once()
        self.assertNotIn('luraph', self.bot.engine_errors)
        self.assertEqual(len(self.uploads), 1)

    async def test_no_engines_at_startup_still_allows_first_use_preparation(self):
        self.bot.engine.prepare = AsyncMock(side_effect=application.ProcessingError('missing'))
        self.bot.luraph_engine.prepare = AsyncMock(side_effect=application.ProcessingError('missing'))
        await self.bot.setup_hook()
        self.assertEqual(set(self.bot.engine_errors), {'deobf', 'luraph'})
        self.bot.luraph_engine.ensure_ready = AsyncMock()
        await self.invoke(self.context())
        self.assertEqual(len(self.uploads), 1)

    @unittest.skipUnless(NATIVE_READY, 'build Luau runtime first')
    async def test_real_engine_attachment_returns_executable_lua(self):
        self.bot.luraph_engine = application.LuraphDeobfuscator(ROOT)
        await self.bot.luraph_engine.prepare()
        source = (ROOT / 'tests' / 'fixtures' / 'vm.lua').read_bytes()
        await self.invoke(self.context(content=source))
        self.assertEqual(len(self.uploads), 1, self.messages)
        filename, recovered = self.uploads[0]
        self.assertEqual(filename, 'resultado.lua')
        with tempfile.TemporaryDirectory() as folder:
            result = Path(folder) / filename
            result.write_bytes(recovered)
            runtime = ROOT / 'vendor' / 'caomod2077' / 'bin' / ('luau.exe' if os.name == 'nt' else 'luau')
            output = subprocess.check_output([str(runtime), str(result)])
        self.assertEqual(output.strip(), b'42')
        self.assertNotIn('DISCORD_TOKEN', self.bot.luraph_engine.environment)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX descendant process cleanup')
    async def test_timeout_kills_descendant(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / 'luraph'
            package.mkdir()
            (package / '__init__.py').touch()
            marker = root / 'escaped'
            child = 'import time; from pathlib import Path; time.sleep(1); Path(' + repr(str(marker)) + ').touch()'
            (package / '__main__.py').write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' + repr(child) + '])\ntime.sleep(20)\n')
            source = root / 'input.lua'
            source.write_text('input')
            engine = application.LuraphDeobfuscator(root)
            with self.assertRaisesRegex(application.ProcessingError, 'tiempo'):
                await engine.run(source, timeout=0.2)
            await asyncio.sleep(1.1)
            self.assertFalse(marker.exists())
            self.assertIsNone(engine.process)
            await engine.close()

    @unittest.skipIf(os.name == 'nt', 'POSIX worker sessions')
    async def test_timeout_stops_fusion_worker_in_its_own_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            package = root / 'luraph'
            package.mkdir()
            (package / '__init__.py').touch()
            (package / 'cli.py').write_text((ROOT / 'luraph' / 'cli.py').read_text())
            marker = root / 'escaped-worker'
            child = 'import time; from pathlib import Path; time.sleep(1); Path(' + repr(str(marker)) + ').touch()'
            (package / '__main__.py').write_text('import sys\nfrom .cli import run_process\nrun_process([sys.executable,"-c",' + repr(child) + '],20)\n')
            source = root / 'input.lua'
            source.write_text('input')
            engine = application.LuraphDeobfuscator(root)
            with self.assertRaisesRegex(application.ProcessingError, 'tiempo'):
                await engine.run(source, timeout=0.3)
            await asyncio.sleep(1.1)
            self.assertFalse(marker.exists())
            await engine.close()

    async def test_false_trace_or_hash_mismatch_cannot_be_delivered(self):
        for kind, digest in [('trace', None), ('lifted', 'wrong')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                package = root / 'luraph'
                package.mkdir()
                (package / '__init__.py').touch()
                script = '''import sys,json,hashlib
from pathlib import Path
out=Path(sys.argv[sys.argv.index('-o')+1]);out.write_bytes(b'print(42)')
report={'selected':{'kind':KIND,'sha256':DIGEST or hashlib.sha256(out.read_bytes()).hexdigest()}}
Path(str(out)+'.json').write_text(json.dumps(report))
'''.replace('KIND', repr(kind)).replace('DIGEST', repr(digest))
                (package / '__main__.py').write_text(script)
                source = root / 'input.lua'
                source.write_text('input')
                engine = application.LuraphDeobfuscator(root)
                with self.assertRaisesRegex(application.ProcessingError, 'verificar'):
                    await engine.run(source)
                await engine.close()

    async def test_first_use_runs_preparation_without_discord_credentials(self):
        engine = application.LuraphDeobfuscator(ROOT)
        engine.prepare = AsyncMock(side_effect=[application.ProcessingError('missing runtime'), None])
        process = SimpleNamespace(pid=123, returncode=0, wait=AsyncMock(return_value=0))
        with patch.object(application.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)) as spawn, \
                patch.object(application.os, 'killpg'):
            await engine.ensure_ready()
        command = spawn.call_args.args
        self.assertIn(str(ROOT / 'prepare_engine.py'), command)
        self.assertIn('--skip-prometheus', command)
        self.assertIn('--skip-dependencies', command)
        self.assertEqual(spawn.call_args.kwargs['env']['LURAPH_RECOVER_DIR'], str(ROOT))
        self.assertNotIn('DISCORD_TOKEN', spawn.call_args.kwargs['env'])
        self.assertEqual(engine.prepare.await_count, 2)
        await engine.close()

    async def test_prepare_rechecks_path_after_bot_was_created(self):
        engine = application.LuraphDeobfuscator()
        engine.repo = ROOT / 'missing-at-startup'
        with patch.object(application, 'find_luraph_directory', return_value=ROOT):
            await engine.prepare()
        self.assertEqual(engine.repo, ROOT)
        await engine.close()

    async def test_shutdown_rejects_new_work(self):
        engine = application.LuraphDeobfuscator(ROOT)
        await engine.close()
        with self.assertRaisesRegex(application.ProcessingError, 'Reinicia'):
            await engine.run(ROOT / 'tests' / 'fixtures' / 'vm.lua')


class PreparationTests(unittest.TestCase):
    def test_original_prometheus_patch_is_preserved_and_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'src').mkdir()
            (root / 'bin').mkdir()
            validate = root / 'src' / 'validate.js'
            validate.write_text(prepare_engine.PROMETHEUS_OLD_VALIDATION + 'return compiler;\n}\n')
            recover = root / 'bin' / 'recover.js'
            recover.write_text("keep();\nwrite(path.join(destination, 'report.json'));\nwrite(path.join(destination, 'strings.json'));\n")
            prepare_engine.prepare_prometheus(root)
            self.assertEqual(validate.read_text(), prepare_engine.PROMETHEUS_NEW_VALIDATION + 'return compiler;\n}\n')
            self.assertEqual(recover.read_text(), 'keep();\n')
            before = validate.read_bytes()
            prepare_engine.prepare_prometheus(root)
            self.assertEqual(validate.read_bytes(), before)

    def test_existing_checkout_builds_and_installs_bot_dependencies(self):
        with patch.object(prepare_engine, 'luraph_directory', return_value=ROOT), \
                patch.object(prepare_engine.subprocess, 'run') as run:
            prepare_engine.main(['--jobs', '2', '--skip-prometheus'])
        self.assertEqual(run.call_count, 2)
        self.assertIn('pip', run.call_args_list[0].args[0])
        self.assertIn(str(ROOT / 'scripts' / 'build_runtime.py'), run.call_args_list[1].args[0])

    def test_resolvers_find_engine_in_working_directory_and_match_relative_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = root / 'work' / 'luraph-fusion'
            (engine / 'luraph').mkdir(parents=True)
            (engine / 'luraph' / 'cli.py').touch()
            standalone = root / 'scripts' / 'bot.py'
            with patch.object(application, '__file__', str(standalone)), \
                    patch.object(prepare_engine, '__file__', str(standalone.with_name('prepare_engine.py'))), \
                    patch.object(Path, 'cwd', return_value=root / 'work'), \
                    patch.dict(os.environ, {'LURAPH_RECOVER_DIR': ''}):
                self.assertEqual(application.find_luraph_directory(), engine)
                self.assertEqual(prepare_engine.luraph_directory(), engine)
                with patch.dict(os.environ, {'LURAPH_RECOVER_DIR': '../chosen'}):
                    expected = (root / 'chosen').resolve()
                    self.assertEqual(application.find_luraph_directory(), expected)
                    self.assertEqual(prepare_engine.luraph_directory(), expected)

    def test_bot_bootstrap_can_build_without_reinstalling_loaded_dependencies(self):
        with patch.object(prepare_engine, 'luraph_directory', return_value=ROOT), \
                patch.object(prepare_engine.subprocess, 'run') as run:
            prepare_engine.main(['--skip-prometheus', '--skip-dependencies'])
        self.assertEqual(run.call_count, 1)
        self.assertIn(str(ROOT / 'scripts' / 'build_runtime.py'), run.call_args.args[0])

    def test_empty_destination_is_accepted_for_download(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)
            with patch.object(prepare_engine, 'luraph_directory', return_value=destination), \
                    patch.object(prepare_engine.subprocess, 'run') as run:
                prepare_engine.main(['--skip-prometheus'])
            self.assertIn('clone', run.call_args_list[0].args[0])

    def test_standalone_preparation_downloads_pinned_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / 'engine'
            with patch.object(prepare_engine, 'luraph_directory', return_value=destination), \
                    patch.object(prepare_engine.subprocess, 'run') as run:
                prepare_engine.main(['--skip-prometheus'])
            self.assertEqual(run.call_count, 4)
            self.assertIn('clone', run.call_args_list[0].args[0])
            self.assertIn(prepare_engine.ENGINE_REVISION, run.call_args_list[1].args[0])


if __name__ == '__main__':
    unittest.main()
