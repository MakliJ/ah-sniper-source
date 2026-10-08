"""A first scan can bootstrap names; schema supports registration/preset fields."""
import ast
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SchemaContractTest(unittest.TestCase):
    def test_web_user_registration_fields_exist_in_creation_schema(self):
        tree = ast.parse((ROOT / 'desktop/main.py').read_text(encoding='utf-8'))
        required = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '_supabase_web'
                    and len(node.args) > 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'POST'
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == 'web_users'
                    and isinstance(node.args[2], ast.Dict)):
                required.update(key.value for key in node.args[2].keys if isinstance(key, ast.Constant))
        self.assertTrue(required)
        schema = (ROOT / 'tools/supabase_web_users.sql').read_text(encoding='utf-8')
        body = re.search(r'CREATE TABLE IF NOT EXISTS public\.web_users\s*\((.*?)\n\);', schema, re.S)[1]
        declared = set(re.findall(r'^\s*([a-z_]+)\s+[A-Z]', body, re.M))
        self.assertFalse(required - declared, f'Registration fields absent from SQL: {required - declared}')


class CatalogBootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        spec = importlib.util.spec_from_file_location('update_items_patch', ROOT / 'tools/update_items_patch.py')
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'CLIENT_ID': 'bootstrap-fixture', 'CLIENT_SECRET': 'bootstrap-fixture'}):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.folder)
        self.module.DB_FILE = 'auction_data.db'
        with closing(sqlite3.connect(self.module.DB_FILE)) as connection:
            connection.execute('CREATE TABLE auction_snapshots (item_id INTEGER)')
            connection.execute('INSERT INTO auction_snapshots VALUES (101)')
            connection.commit()

    def test_missing_optional_metadata_table_and_env_do_not_block_ids(self):
        self.assertEqual(self.module.collect_ids(), [101])

    def test_first_scan_bootstraps_absent_json_dictionaries(self):
        with closing(sqlite3.connect(self.module.DB_FILE)) as connection:
            connection.execute('CREATE TABLE item_meta (item_id INTEGER)')
        (self.folder / '.env').write_text('', encoding='utf-8')
        with (patch.object(self.module, 'run_phase_names', return_value=({'101':'Item'}, {'101':'Предмет'})) as names,
             patch.object(self.module, 'run_phase_icons', return_value={'101':'icon'}) as icons,
             patch.object(self.module, 'run_phase_db') as database):
            self.module.main()
        names.assert_called_once_with({}, {}, [101])
        icons.assert_called_once_with([101])
        database.assert_called_once_with({'101':'Item'}, {'101':'Предмет'})

    def test_corrupt_existing_names_are_not_replaced_with_empty_defaults(self):
        with closing(sqlite3.connect(self.module.DB_FILE)) as connection:
            connection.execute('CREATE TABLE item_meta (item_id INTEGER)')
        (self.folder / '.env').write_text('', encoding='utf-8')
        (self.folder / 'item_names.json').write_text('broken JSON', encoding='utf-8')
        with patch.object(self.module, 'run_phase_names') as fetch:
            with self.assertRaises(json.JSONDecodeError):
                self.module.main()
            fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
