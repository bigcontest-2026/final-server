"""Startup selection must never seed demo data over a configured remote source."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/run_api.py'


def load_launcher():
    spec = importlib.util.spec_from_file_location('hermes_supabase_startup_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SupabaseStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.backend = self.root / 'service/backend'
        self.backend.mkdir(parents=True)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.path_patch = patch.object(sys, 'path', list(sys.path))
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.create_demo = Mock(side_effect=lambda path: Path(path).touch())
        self.modules = patch.dict(sys.modules, {
            'donghaeng_agent.welfare_erd': SimpleNamespace(create_demo=self.create_demo),
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.launcher = load_launcher()

    def write_env(self, content):
        (self.backend / '.env').write_text(content, encoding='utf-8')

    def test_import_does_not_load_environment_or_create_demo(self):
        with patch('dotenv.load_dotenv') as load:
            load_launcher()
        load.assert_not_called()
        self.create_demo.assert_not_called()
        self.assertNotIn('WELFARE_PUBLICATION_ID', os.environ)

    def test_env_file_remote_disables_demo_and_demo_publication_default(self):
        self.write_env('WELFARE_DATABASE_URL="postgresql://user:secret@example.test/postgres"\n')
        self.assertIsNone(self.launcher.configure(self.root))
        self.create_demo.assert_not_called()
        self.assertNotIn('WELFARE_DB_PATH', os.environ)
        self.assertNotIn('WELFARE_PUBLICATION_ID', os.environ)
        self.assertFalse((self.root / 'runtime/welfare_erd_v2.sqlite').exists())

    def test_exported_settings_take_precedence_over_env_file(self):
        os.environ.update(WELFARE_DATABASE_URL='postgresql://exported.example/db',
                          WELFARE_PUBLICATION_ID='selected-corpus')
        self.write_env('WELFARE_DATABASE_URL="postgresql://file.example/db"\n'
                       'WELFARE_PUBLICATION_ID="file-corpus"\n')
        self.launcher.configure(self.root)
        self.assertEqual(os.environ['WELFARE_DATABASE_URL'], 'postgresql://exported.example/db')
        self.assertEqual(os.environ['WELFARE_PUBLICATION_ID'], 'selected-corpus')
        self.create_demo.assert_not_called()

    def test_password_is_not_interpolated(self):
        url = 'postgresql://user:${SECRET}@example.test/postgres'
        os.environ['SECRET'] = 'must-not-replace-password'
        self.write_env(f'WELFARE_DATABASE_URL="{url}"\n')
        self.launcher.configure(self.root)
        self.assertEqual(os.environ['WELFARE_DATABASE_URL'], url)

    def test_no_remote_settings_preserves_local_demo_startup(self):
        expected = self.root / 'runtime/welfare_erd_v2.sqlite'
        self.assertEqual(self.launcher.configure(self.root), expected)
        self.create_demo.assert_called_once_with(expected)
        self.assertEqual(os.environ['WELFARE_PUBLICATION_ID'], 'erd-demo-v2')
        self.assertEqual(self.launcher.configure(self.root), expected)
        self.create_demo.assert_called_once()

    def test_blank_remote_url_preserves_explicit_local_settings(self):
        existing = self.root / 'existing.sqlite'
        existing.touch()
        self.write_env(f'WELFARE_DATABASE_URL=""\nWELFARE_DB_PATH="{existing}"\n'
                       'WELFARE_PUBLICATION_ID="local-custom"\n')
        self.assertEqual(self.launcher.configure(self.root), existing)
        self.assertEqual(os.environ['WELFARE_PUBLICATION_ID'], 'local-custom')
        self.create_demo.assert_not_called()


if __name__ == '__main__':
    unittest.main()
