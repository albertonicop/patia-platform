"""Mandatory owner setup supersedes the optional update notice."""
import unittest
from unittest.mock import patch
from sqlalchemy.exc import OperationalError

from app import db
from app.models import OrganizationMember, Product, User
from app.team.services import ROLE_PERMISSIONS
from app.work_style import OwnerWorkPreference, OwnerWorkUpdateNotice
from test_cashier_session import CashierFixture


class RequiredOwnerSetupTests(unittest.TestCase):
    def setUp(self):
        self.audit = CashierFixture()
        self.addCleanup(self.audit.close)
        self.client = self.audit.owner_client
        db.session.delete(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        db.session.commit()

    def test_missing_pending_deferred_and_previously_seen_notice_require_setup(self):
        for mode, action in [(None, None), ('pending', 'seen'), ('deferred', 'later')]:
            with self.subTest(mode=mode):
                preference = db.session.get(OwnerWorkPreference, self.audit.owner.id)
                if preference:
                    db.session.delete(preference)
                notice = db.session.get(OwnerWorkUpdateNotice, self.audit.owner.id)
                if notice:
                    db.session.delete(notice)
                if mode:
                    db.session.add(OwnerWorkPreference(user_id=self.audit.owner.id, mode=mode))
                if action:
                    db.session.add(OwnerWorkUpdateNotice(user_id=self.audit.owner.id, action=action))
                db.session.commit()
                self.assertEqual(self.client.get('/').location, '/settings/work-style/setup')
                self.assertEqual(self.client.get('/products').location, '/settings/work-style/setup')
        html = self.client.get('/settings/work-style/setup').get_data(as_text=True)
        for text in ('Personaliza tu espacio de trabajo', 'Guardar y entrar', 'Ayuda', 'Cerrar sesión'):
            self.assertIn(text, html)
        for text in ('Más tarde', 'Omitir', 'Configurar después', 'Menú completo actual'):
            self.assertNotIn(text, html)
        nav = html.split('id="primary-navigation"', 1)[1].split('</nav>', 1)[0]
        self.assertNotIn('href="/sell"', nav)

    def test_direct_get_post_and_json_routes_block_before_mutations(self):
        stock = self.audit.product.stock
        for route in ('/', '/sell', '/products', '/settings', '/team', '/reports', '/cash-register',
                      '/subscription', '/pro/hub', '/recipes', '/download-template'):
            self.assertEqual(self.client.get(route).location, '/settings/work-style/setup', route)
        response = self.client.post('/sell-cart', json={'items': [{'product_id': self.audit.product.id, 'quantity':1}]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error_code'], 'work_style_required')
        self.assertEqual(response.json['setup_url'], '/settings/work-style/setup')
        self.assertEqual(self.client.post('/cash-register/open', data={'opening_cash':'100'}).status_code, 303)
        self.assertEqual(self.client.post('/products/new', data={'name':'Must not exist'}).status_code, 303)
        db.session.refresh(self.audit.product)
        self.assertEqual(self.audit.product.stock, stock)
        self.assertEqual(Product.query.count(), 1)

    def test_public_pages_callbacks_static_and_logout_are_not_setup_gated(self):
        for route in ('/terminos', '/privacidad', '/login', '/static/css/work-style.css'):
            response = self.client.get(route)
            self.assertNotEqual(response.location, '/settings/work-style/setup', route)
        self.assertNotEqual(self.client.post('/stripe-webhook', data='{}', content_type='application/json').status_code, 409)
        self.assertNotEqual(self.client.get('/stripe-success').location, '/settings/work-style/setup')
        anonymous = self.audit.app.test_client()
        self.assertEqual(anonymous.get('/').status_code, 200)
        self.assertEqual(self.client.post('/logout').status_code, 302)

    def test_completed_choices_persist_and_remove_dashboard_priority_block(self):
        roles = {member.id: member.role for member in OrganizationMember.query.all()}
        plan = (self.audit.owner.plan, self.audit.owner.manual_pro_access, self.audit.owner.subscription_plan_code)
        permissions = dict(ROLE_PERMISSIONS)
        for style, activity, mode in [('solo', '', 'solo'), ('team', 'operations', 'team_operations'), ('team', 'supervision', 'team_supervision')]:
            self.assertEqual(self.client.post('/settings/work-style', data={
                'work_style':style, 'owner_activity':activity, 'source':'onboarding'}).status_code, 303)
            self.assertEqual(db.session.get(OwnerWorkPreference, self.audit.owner.id).mode, mode)
            html = self.client.get('/').get_data(as_text=True)
            self.assertNotIn('work-style-priorities', html)
            self.assertNotIn('Cambiar forma de trabajo', html)
            self.assertNotIn('data-work-style-form', html)
            self.assertIn('data-work-nav="pos"', html)
            self.assertEqual(self.client.get('/sell').status_code, 200)
            self.assertEqual(self.client.get('/cash-register').status_code, 200)
            self.client.post('/logout')
            self.client = self.audit.app.test_client()
            self.client.post('/login', data={'email':self.audit.owner.email, 'password':'Password123'})
            self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual({member.id:member.role for member in OrganizationMember.query.all()}, roles)
        self.assertEqual((self.audit.owner.plan, self.audit.owner.manual_pro_access, self.audit.owner.subscription_plan_code), plan)
        self.assertEqual(ROLE_PERMISSIONS, permissions)
        self.assertIn('Forma de trabajo', self.client.get('/settings').get_data(as_text=True))

    def test_staff_are_not_blocked_even_if_the_owner_has_not_completed(self):
        manager, _ = self.audit.fixture.add_member('MANAGER', 'required-manager@example.com')
        manager_client = self.audit.app.test_client()
        manager_client.post('/login', data={'email':manager.email, 'password':'Password123'})
        self.assertEqual(manager_client.get('/').status_code, 200)
        self.assertEqual(self.audit.client.get('/').location, '/sell')
        self.assertEqual(self.audit.client.get('/sell').status_code, 200)
        for client in (manager_client, self.audit.client):
            self.assertEqual(client.get('/settings/work-style/setup').status_code, 403)
            self.assertEqual(client.post('/settings/work-style', data={'work_style':'solo'}).status_code, 403)
        self.assertEqual(self.audit.client.get('/reports').status_code, 403)
        self.assertEqual(manager_client.get('/team').status_code, 403)

    def test_invalid_input_and_retired_actions_cannot_bypass(self):
        for data in ({'work_style':'deferred'}, {'work_style':'solo', 'action':'defer'},
                     {'work_style':'team'}, {'work_style':'team', 'owner_activity':'OWNER'}):
            self.assertEqual(self.client.post('/settings/work-style', data=data).status_code, 422)
            self.assertIsNone(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        self.assertEqual(self.client.post('/settings/work-style/update', data={'action':'later'}).location, '/settings/work-style/setup')

    def test_failed_commit_preserves_selection_and_can_retry(self):
        selection = {'work_style':'team', 'owner_activity':'supervision', 'source':'onboarding'}
        with patch.object(db.session, 'commit', side_effect=OperationalError('fictitious failure', {}, Exception('test'))):
            response = self.client.post('/settings/work-style', data=selection)
        self.assertEqual(response.status_code, 503)
        html = response.get_data(as_text=True)
        self.assertIn('No pudimos guardar', html)
        self.assertIn('value="team" required checked', html)
        self.assertIn('value="supervision" checked', html)
        self.assertIsNone(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        self.assertEqual(self.client.get('/').location, '/settings/work-style/setup')
        self.assertEqual(self.client.post('/settings/work-style', data=selection).status_code, 303)
        self.assertEqual(self.client.get('/').status_code, 200)
