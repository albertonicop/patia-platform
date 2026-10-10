"""Preferences alter presentation only; isolated owners and employees."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from flask import url_for

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from app import db
from app.models import OrganizationInvitation, OrganizationMember, User
from app.team.services import ROLE_PERMISSIONS
from app.work_style import OwnerWorkPreference, initialize_new_owner
from test_cashier_session import CashierFixture


class OwnerWorkStyleTests(unittest.TestCase):
    def setUp(self):
        self.audit = CashierFixture()
        self.addCleanup(self.audit.close)
        self.client = self.audit.owner_client

    def save(self, **values):
        return self.client.post('/settings/work-style', data=values)

    def test_legacy_owner_keeps_menu_and_only_new_registration_gets_question(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('data-work-style-form', html)
        self.assertNotIn('data-work-nav=', html)
        self.assertIsNone(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        initialize_new_owner(self.audit.owner)
        db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        self.assertEqual(html.count('¿Cómo trabajas en tu negocio?'), 1)
        self.assertEqual(html.count('id="onboarding-title"'), 1)
        self.assertEqual(html.count('data-work-style-form'), 1)
        self.assertIn('Configurar después', html)

    def test_choices_persist_and_preserve_tools_roles_plan_and_employees(self):
        audit = self.audit
        initial_roles = {m.id: m.role for m in OrganizationMember.query.all()}
        initial_plan = (audit.owner.plan, audit.owner.subscription_plan_code, audit.owner.trial_plan_code)
        initial_permissions = {key: value for key, value in ROLE_PERMISSIONS.items()}
        for style, activity, mode, first in [('solo', '', 'solo', ['home','pos','inventory','cash']),
                ('team','operations','team_operations',['home','pos','inventory','cash']),
                ('team','supervision','team_supervision',['home','reports','decisions'])]:
            with self.subTest(mode=mode):
                response = self.save(work_style=style, owner_activity=activity, user_id=999999, role='CASHIER', plan='RESTAURANT')
                self.assertEqual(response.status_code, 303)
                self.assertEqual(db.session.get(OwnerWorkPreference, audit.owner.id).mode, mode)
                audit.owner_client.post('/logout')
                response = audit.owner_client.post('/login', data={'email':audit.owner.email,'password':'Password123'})
                self.assertEqual(response.location, '/')
                html = self.client.get('/').get_data(as_text=True)
                import re
                nav = html.split('id="primary-navigation"',1)[1].split('</nav>',1)[0]
                order = re.findall(r'data-work-nav="([^"]+)"', nav)
                self.assertEqual(order[:len(first)], first)
                self.assertEqual(len(order), len(set(order)))
                for tool in ('home','pos','inventory','cash','reports','decisions','settings','team','subscription','customers','credit','suppliers','purchases'):
                    self.assertIn(tool, order)
                self.assertNotIn('data-work-style-form', html)
                self.assertEqual(self.client.get('/sell').status_code, 200)
                self.assertEqual(self.client.get('/cash-register').status_code, 200)
        self.assertEqual({m.id:m.role for m in OrganizationMember.query.all()}, initial_roles)
        self.assertEqual(OrganizationInvitation.query.count(), 1)
        self.assertEqual((audit.owner.plan,audit.owner.subscription_plan_code,audit.owner.trial_plan_code), initial_plan)
        self.assertEqual(ROLE_PERMISSIONS, initial_permissions)
        self.assertIn('Forma de trabajo', self.client.get('/settings').get_data(as_text=True))
        self.assertEqual(self.save(work_style='team',owner_activity='supervision',action='team').location, '/team')

    def test_defer_is_permanent_until_changed_and_invalid_payload_does_not_save(self):
        initialize_new_owner(self.audit.owner)
        db.session.commit()
        response = self.save(work_style='team', owner_activity='OWNER', source='onboarding')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(db.session.get(OwnerWorkPreference,self.audit.owner.id).mode, 'pending')
        self.save(action='defer', source='onboarding')
        self.client.post('/logout')
        self.client.post('/login',data={'email':self.audit.owner.email,'password':'Password123'})
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('data-work-style-form',html)
        self.assertNotIn('data-work-nav=',html)
        self.assertEqual(db.session.get(OwnerWorkPreference,self.audit.owner.id).mode,'deferred')
        self.save(work_style='solo')
        self.assertEqual(db.session.get(OwnerWorkPreference,self.audit.owner.id).mode,'solo')
        self.save(work_style='deferred')
        self.assertNotIn('data-work-nav=',self.client.get('/').get_data(as_text=True))

    def test_employees_do_not_inherit_owner_preferences_or_platform_admin(self):
        audit=self.audit
        self.save(work_style='team',owner_activity='supervision')
        manager, _ = audit.fixture.add_member('MANAGER','manager.demo@example.com')
        manager_client = audit.app.test_client()
        manager_client.post('/login',data={'email':manager.email,'password':'Password123'})
        self.assertEqual(manager_client.get('/').status_code,200)
        self.assertEqual(audit.client.get('/').location,'/sell')
        for client in (audit.client,manager_client):
            html=client.get('/sell').get_data(as_text=True)
            self.assertNotIn('data-work-nav=',html)
            self.assertNotIn('data-work-style-form',html)
            self.assertNotIn('href="/admin"',html)
            self.assertIn('Ayuda',html)
            self.assertIn('Cerrar sesión',html)
            self.assertEqual(client.post('/settings/work-style',data={'work_style':'solo','user_id':audit.owner.id}).status_code,403)
            for route in ('/settings','/team','/subscription'):
                self.assertEqual(client.get(route).status_code,403)
            self.assertNotEqual(client.get('/admin').status_code,200)
        cashier_html = audit.client.get('/sell').get_data(as_text=True)
        for route in ('/reports','/products','/pro/hub'):
            self.assertNotIn(f'href="{route}"',cashier_html)
            self.assertEqual(audit.client.get(route).status_code,403)
        manager_html = manager_client.get('/sell').get_data(as_text=True)
        self.assertIn('href="/products"',manager_html)
        self.assertIn('href="/reports"',manager_html)
        self.assertEqual(manager_client.get('/reports').status_code,200)
        self.assertEqual(db.session.get(OwnerWorkPreference,audit.owner.id).mode,'team_supervision')
        self.assertIsNone(db.session.get(OwnerWorkPreference,audit.cashier.id))

    def test_platform_allowlist_and_other_owner_isolation(self):
        audit=self.audit
        self.save(work_style='solo')
        other, _ = audit.fixture.add_owner('other-owner.demo@example.com')
        other_client = audit.app.test_client()
        other_client.post('/login',data={'email':other.email,'password':'Password123'})
        other_client.post('/settings/work-style',data={'work_style':'team','owner_activity':'supervision','user_id':audit.owner.id})
        self.assertEqual(db.session.get(OwnerWorkPreference,audit.owner.id).mode,'solo')
        self.assertEqual(db.session.get(OwnerWorkPreference,other.id).mode,'team_supervision')
        # Enumerate real platform endpoints, rather than mistaking a typo/404
        # for a permission check. No endpoint may run for another owner.
        with audit.app.test_request_context():
            targets = [(url_for(rule.endpoint, **{key:99999 for key in rule.arguments}),
                        'post' if 'POST' in rule.methods else 'get')
                       for rule in audit.app.url_map.iter_rules() if rule.rule.startswith('/admin')]
        self.assertGreaterEqual(len(targets),7)
        for route, method in targets:
            self.assertIn(getattr(other_client,method)(route).status_code,(302,403),route)
        admin, _ = audit.fixture.add_owner('albertonicopat@gmail.com')
        admin_client=audit.app.test_client()
        admin_client.post('/login',data={'email':admin.email,'password':'Password123'})
        self.assertIn('href="/admin"',admin_client.get('/sell').get_data(as_text=True))
        self.assertEqual(admin_client.get('/admin').status_code,200)

    def test_real_registration_initializes_preference_before_verification(self):
        client=self.audit.app.test_client()
        with patch('app.routes.send_email',return_value=True), patch('app.routes.validate_email'):
            response=client.post('/register',data={'email':'new.demo@example.com','password':'Password123',
                'first_name':'Nuevo','last_name':'Demo','company_name':'Negocio nuevo demo','phone':'5555555555',
                'address':'Calle ficticia 1','city':'Puebla','state':'Puebla','postal_code':'72000','business_type':'general'})
        self.assertEqual(response.status_code,302)
        user=User.query.filter_by(email='new.demo@example.com').one()
        self.assertEqual(db.session.get(OwnerWorkPreference,user.id).mode,'pending')
        self.assertIn('/verify-email',client.get('/').location)
        client.post('/verify-email',data={'code':user.verification_code})
        self.assertIn('data-work-style-form',client.get('/').get_data(as_text=True))


class OwnerWorkStyleMigrationTests(unittest.TestCase):
    def test_migration_leaves_legacy_owners_unchanged_and_enforces_modes(self):
        path=Path(__file__).resolve().parents[1]/'migrations/versions/20261010_28_owner_work_preference.py'
        spec=importlib.util.spec_from_file_location('work_style_migration',path)
        migration=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with tempfile.TemporaryDirectory(prefix='patia-work-migration-') as directory:
            engine=sa.create_engine('sqlite:///'+str(Path(directory)/'test.db'))
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql('CREATE TABLE user (id INTEGER PRIMARY KEY, email TEXT NOT NULL)')
                    connection.exec_driver_sql("INSERT INTO user VALUES (1,'fictitious@example.com')")
                    with Operations.context(MigrationContext.configure(connection)):
                        migration.upgrade()
                    self.assertEqual(connection.exec_driver_sql('SELECT COUNT(*) FROM owner_work_preference').scalar_one(),0)
                    with connection.begin_nested():
                        with self.assertRaises(sa.exc.IntegrityError):
                            connection.exec_driver_sql("INSERT INTO owner_work_preference VALUES (1,'OWNER','2026-10-10')")
                    self.assertEqual(connection.exec_driver_sql('SELECT email FROM user').scalar_one(),'fictitious@example.com')
                    with Operations.context(MigrationContext.configure(connection)):
                        migration.downgrade()
                    self.assertNotIn('owner_work_preference',sa.inspect(connection).get_table_names())
            finally:
                engine.dispose()
