"""Migrate a legacy disposable SQLite database without reusing sale IDs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


class TicketReversalMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_sale_and_prevents_reused_id(self):
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/20261010_27_reversed_ticket_lines.py'
        spec = importlib.util.spec_from_file_location('ticket_reversal_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with tempfile.TemporaryDirectory(prefix='patia-return-migration-') as directory:
            engine = sa.create_engine('sqlite:///' + str(Path(directory) / 'migration.db'))
            try:
                with engine.begin() as connection:
                    for table in ('organization', 'sales_ticket', 'organization_member'):
                        connection.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
                        connection.exec_driver_sql(f'INSERT INTO {table} (id) VALUES (1)')
                    connection.exec_driver_sql('CREATE TABLE sale (id INTEGER PRIMARY KEY, quantity INTEGER NOT NULL, total NUMERIC(14,2) NOT NULL)')
                    connection.exec_driver_sql('INSERT INTO sale VALUES (1, 2, 20.00)')
                    with Operations.context(MigrationContext.configure(connection)):
                        migration.upgrade()
                    self.assertEqual(connection.exec_driver_sql('SELECT * FROM sale').one(), (1, 2, 20))
                    connection.exec_driver_sql("""INSERT INTO reversed_sale_line
                        (organization_id, original_sale_id, sales_ticket_id, product_name,
                         quantity, unit_price, total, created_at, currency_code, locale_code,
                         reversal_type, reversed_at, performed_by_member_id)
                        VALUES (1,1,1,'Fictitious product',2,10,20,'2026-10-10','MXN',
                                'es_MX','RETURN','2026-10-10',1)""")
                    connection.exec_driver_sql('DELETE FROM sale WHERE id=1')
                    connection.exec_driver_sql('INSERT INTO sale (quantity,total) VALUES (1,10)')
                    self.assertEqual(connection.exec_driver_sql('SELECT id FROM sale').scalar_one(), 2)
                    self.assertEqual(connection.exec_driver_sql('SELECT original_sale_id,total FROM reversed_sale_line').one(), (1, 20))
                    with Operations.context(MigrationContext.configure(connection)):
                        migration.downgrade()
                    self.assertNotIn('reversed_sale_line', sa.inspect(connection).get_table_names())
                    self.assertEqual(connection.exec_driver_sql('SELECT quantity,total FROM sale').one(), (1, 10))
            finally:
                engine.dispose()
