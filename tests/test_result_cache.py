import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from result_cache import ResultStore, engine_namespace


class PersistentCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def store(self, namespace='one', **kwargs):
        return ResultStore(self.directory, namespace, **kwargs)

    def test_verified_output_survives_new_instance(self):
        self.store().put(b'key', b'print(42)')
        self.assertEqual(self.store().get(b'key'), b'print(42)')
        self.assertIsNone(self.store('other-engine').get(b'key'))

    def test_tampering_or_truncated_output_is_rejected(self):
        store = self.store()
        for altered in (b'print(24)', b''):
            store.put(b'key', b'print(42)')
            with sqlite3.connect(store.path) as db:
                db.execute('UPDATE results SET value=?', (altered,))
            self.assertIsNone(store.get(b'key'))

    def test_byte_and_entry_limits_evict_oldest_across_namespaces(self):
        store = self.store(max_bytes=8, max_entries=2)
        store.put(b'a', b'aaa')
        store.put(b'b', b'bbb')
        self.assertEqual(store.get(b'a'), b'aaa')
        store.put(b'c', b'ccc')
        self.assertIsNone(store.get(b'b'))
        self.assertEqual(store.get(b'a'), b'aaa')
        self.store('second', max_bytes=8, max_entries=2).put(b'd', b'ddd')
        with sqlite3.connect(store.path) as db:
            size, count = db.execute('SELECT sum(size),count(*) FROM results').fetchone()
        self.assertLessEqual(size, 8)
        self.assertLessEqual(count, 2)

    def test_oversized_empty_and_failed_disk_writes_do_not_break_processing(self):
        store = self.store(max_bytes=8)
        store.put(b'large', b'123456789')
        store.put(b'empty', b'')
        self.assertIsNone(store.get(b'large'))
        self.assertIsNone(store.get(b'empty'))
        store.path.unlink()
        store.path.write_bytes(b'corrupted database')
        store.put(b'normal', b'ok')
        self.assertIsNone(store.get(b'normal'))
        bad = self.directory / 'not-directory'
        bad.write_bytes(b'file')
        self.assertFalse(ResultStore(bad, 'one').enabled)

    def test_namespace_tracks_contents_not_installation_path(self):
        engine = self.directory / 'engine'
        source = engine / 'luraph' / 'cli.py'
        source.parent.mkdir(parents=True)
        source.write_text('first engine')
        bot = self.directory / 'bot.py'
        bot.write_text('bot')
        first = engine_namespace(engine, bot)
        source.write_text('second engine')
        self.assertNotEqual(first, engine_namespace(engine, bot))
        source.write_text('first engine')
        binary = engine / 'vendor' / 'caomod2077' / 'bin' / 'luau'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'native runtime')
        self.assertNotEqual(first, engine_namespace(engine, bot))
