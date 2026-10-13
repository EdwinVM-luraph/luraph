"""Lazy constant decoding must preserve trace checks and fallback cleanup."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

CORE = Path(__file__).resolve().parents[1] / 'vendor' / 'caomod2077' / 'core'
sys.path.insert(0, str(CORE))
from obfuscators.luraph_v15 import driver


class LazyHarnessTests(unittest.TestCase):
    def setUp(self):
        self.job = SimpleNamespace(args=SimpleNamespace(timeout=10))
        self.runner = SimpleNamespace(bridge=False, luau='luau', run=Mock(return_value=('fallback', '')))
        self.server = Mock()
        self.server.reply.return_value = ('matching trace', '')
        self.server.fetch.return_value = '{"values": []}'
        self.server.request.return_value = ('next trace', '')
        self.factory = patch.object(driver.harness, 'HarnessServer', return_value=self.server)
        self.factory_mock = self.factory.start()
        self.addCleanup(self.factory.stop)
        self.environ = patch.dict(driver.os.environ, {}, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.trace = patch.object(driver.harness, 'trace_text', side_effect=lambda text: text)
        self.trace.start()
        self.addCleanup(self.trace.stop)

    def run_pipeline(self, callback):
        errors = []
        def checked(*args):
            try:
                callback(*args)
            except BaseException as error:
                errors.append(error)
                raise
        with patch.object(driver, 'devirtualize', side_effect=checked):
            driver.lift(self.job, self.runner, 'source', {}, {}, 'matching trace', 'protos', 'output', [])
        if errors:
            raise errors[0]

    def test_no_constants_never_replays_protected_source(self):
        def pipeline(*args):
            self.assertTrue(callable(args[6]()))
        self.run_pipeline(pipeline)
        self.factory_mock.assert_not_called()
        self.runner.run.assert_not_called()

    def test_requests_share_one_synchronized_session(self):
        def pipeline(*args):
            fetch = args[6]()
            self.assertEqual(fetch(['one'], '='), '{"values": []}')
            fetch(['two'], '=')
        self.run_pipeline(pipeline)
        self.factory_mock.assert_called_once()
        self.server.reply.assert_called_once_with(10)
        self.assertEqual(self.server.fetch.call_count, 2)
        self.server.close.assert_called_once()

    def test_different_trace_rejects_fetch_and_falls_back(self):
        self.server.reply.return_value = ('print(2)', '')
        def pipeline(*args):
            with self.assertRaisesRegex(RuntimeError, 'synchronized'):
                args[6]()(['one'], '=')
            self.assertEqual(args[4]({}), ('fallback', ''))
        self.run_pipeline(pipeline)
        self.server.fetch.assert_not_called()
        self.server.close.assert_called_once()
        self.factory_mock.assert_called_once()
        self.runner.run.assert_called_once()

    def test_dead_server_falls_back_without_leaking(self):
        self.server.reply.return_value = (None, 'timeout')
        def pipeline(*args):
            self.assertEqual(args[4]({}), ('fallback', ''))
        self.run_pipeline(pipeline)
        self.server.close.assert_called_once()
        self.runner.run.assert_called_once()

    def test_failed_request_closes_server_and_falls_back(self):
        self.server.request.return_value = (None, 'timeout')
        def pipeline(*args):
            self.assertEqual(args[4]({}), ('fallback', ''))
        self.run_pipeline(pipeline)
        self.server.close.assert_called_once()
        self.runner.run.assert_called_once()

    def test_disabled_fetch_keeps_normal_constant_rounds(self):
        driver.os.environ['DEOB_NO_FETCH'] = '1'
        def pipeline(*args):
            self.assertIsNone(args[6]())
            self.assertEqual(args[4]({}), ('next trace', ''))
        self.run_pipeline(pipeline)
        self.server.close.assert_called_once()

    def test_disabled_session_uses_runner(self):
        driver.os.environ['DEOB_NO_SERVE'] = '1'
        def pipeline(*args):
            self.assertIsNone(args[6]())
            self.assertEqual(args[4]({}), ('fallback', ''))
        self.run_pipeline(pipeline)
        self.factory_mock.assert_not_called()
        self.runner.run.assert_called_once()
