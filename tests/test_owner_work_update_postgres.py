"""Migration and notice concurrency on a verified disposable PostgreSQL cluster."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

import psycopg2
from psycopg2 import sql
from flask_migrate import upgrade
import sqlalchemy as sa

from app import create_app, db
from app.models import Product, User
from app.team.services import ensure_owner_organization
from app.work_style import OwnerWorkPreference, OwnerWorkUpdateNotice


@unittest.skipUnless(os.environ.get('PATIA_ISOLATED_PG_METADATA'), 'Disposable PostgreSQL cluster not configured')
class OwnerWorkUpdatePostgresTests(unittest.TestCase):
    def connect(self, database='postgres'):
        connection = psycopg2.connect(host='127.0.0.1', port=self.metadata['port'],
                                      user='patia_audit', dbname=database, connect_timeout=5)
        with connection.cursor() as cursor:
            cursor.execute('SHOW data_directory')
            self.assertEqual(Path(cursor.fetchone()[0]).resolve(), self.data_directory)
        connection.rollback()
        return connection

    def setUp(self):
        self.metadata = json.loads(Path(os.environ['PATIA_ISOLATED_PG_METADATA']).read_text())
        root = Path(self.metadata['root']).resolve()
        self.assertTrue(root.name.startswith('patia-pg-isolated-'))
        self.assertEqual(self.metadata['user'], 'patia_audit')
        self.data_directory = (root / 'data').resolve()
        self.database = 'patia_work_update_' + uuid.uuid4().hex + '_test'
        connection = self.connect()
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(self.database)))
        connection.close()
        self.env = dict(DATABASE_URL=f"postgresql://patia_audit@127.0.0.1:{self.metadata['port']}/{self.database}",
                        SECRET_KEY='fictitious-work-update', STRIPE_DISABLED='1', PUBLIC_BASE_URL='https://patia.test')
        with patch.dict(os.environ, self.env):
            self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        connection = self.connect()
        connection.autocommit = True
        with connection.cursor() as cursor:
            self.assertTrue(self.database.startswith('patia_work_update_') and self.database.endswith('_test'))
            cursor.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.database)))
        connection.close()

    def fingerprint(self):
        result = {}
        with db.engine.connect() as connection:
            for table in sa.inspect(connection).get_table_names():
                if table == 'alembic_version':
                    continue
                rows = connection.execute(sa.text('SELECT * FROM "' + table.replace('"', '""') + '"')).all()
                result[table] = hashlib.sha256(repr(sorted(repr(tuple(row)) for row in rows)).encode()).hexdigest()
        return result

    def test_upgrade_preserves_existing_data_and_exact_predeploy_command_succeeds(self):
        with self.app.app_context():
            upgrade(revision='20261010_27')
            owner = User(email='existing-pg-owner@example.com', company_name='Fictitious existing shop',
                         email_verified=True, manual_pro_access=True)
            owner.set_password('FictitiousPassword123')
            configured = User(email='configured-pg-owner@example.com', company_name='Fictitious configured shop',
                              email_verified=True, manual_pro_access=True)
            configured.set_password('FictitiousPassword123')
            db.session.add_all([owner, configured])
            db.session.flush()
            member = ensure_owner_organization(owner)
            ensure_owner_organization(configured)
            db.session.add(Product(organization_id=member.organization_id, user_id=owner.id,
                                   name='Existing fictitious stock', sku='WORK-UPDATE',
                                   cost_price=Decimal('4.00'), sale_price=Decimal('10.00'), stock=30, min_stock=1))
            db.session.commit()
            owner_id, organization_id = owner.id, member.organization_id
            configured_id = configured.id
            before = self.fingerprint()
            upgrade(revision='20261010_28')
            after = self.fingerprint()
            self.assertEqual({name: after[name] for name in before}, before)
            self.assertEqual(OwnerWorkPreference.query.count(), 0)
            db.session.add(OwnerWorkPreference(user_id=configured.id, mode='team_supervision'))
            db.session.commit()
            before = self.fingerprint()
        # Same command configured in render.yaml, exclusively against our own database.
        command = [sys.executable, '-m', 'flask', '--app', 'run.py', 'db', 'upgrade']
        for _ in range(2):
            result = subprocess.run(command, env={**os.environ, **self.env}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.app.app_context():
                after = self.fingerprint()
                self.assertEqual({name: after[name] for name in before}, before)
                self.assertEqual(OwnerWorkUpdateNotice.query.count(), 0)
                self.assertEqual(db.session.execute(sa.text('SELECT version_num FROM alembic_version')).scalar(), '20261010_29')
        def visit():
            client = self.app.test_client()
            with client.session_transaction() as session:
                session['user_id'] = owner_id
                session['organization_id'] = organization_id
            response = client.get('/')
            self.assertEqual(response.status_code, 200)
            return 'data-work-update' in response.get_data(as_text=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: visit(), range(2)))
        self.assertEqual(sum(results), 1)
        with self.app.app_context():
            self.assertEqual(OwnerWorkUpdateNotice.query.count(), 1)
            self.assertIsNone(db.session.get(OwnerWorkPreference, owner_id))
            self.assertEqual(db.session.get(OwnerWorkPreference, configured_id).mode, 'team_supervision')
            with self.connect(self.database) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SHOW server_version')
                    print('ISOLATED_POSTGRES_VERSION=' + cursor.fetchone()[0])
        self.assertFalse(visit())

    def test_render_predeploy_is_before_gunicorn(self):
        config = (Path(__file__).resolve().parents[1] / 'render.yaml').read_text()
        self.assertIn('preDeployCommand: python -m flask --app run.py db upgrade', config)
        self.assertIn('startCommand: gunicorn run:app', config)
