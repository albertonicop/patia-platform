"""An existing subscribed owner's update is account-scoped, not device-scoped."""
from datetime import datetime, timedelta
import unittest

from app import db
from app.work_style import OwnerWorkPreference, OwnerWorkUpdateNotice, initialize_new_owner
from test_cashier_session import CashierFixture


class OwnerWorkUpdateTests(unittest.TestCase):
    def setUp(self):
        self.audit = CashierFixture()
        self.addCleanup(self.audit.close)
        self.client = self.audit.owner_client

    def fresh_device(self):
        client = self.audit.app.test_client()
        self.assertEqual(client.post('/login', data={
            'email': self.audit.owner.email, 'password': 'Password123'}).status_code, 302)
        return client

    def test_seen_once_after_logout_and_on_another_device_without_changing_menu(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('data-work-update', html)
        self.assertIn('Actualización de PATIA', html)
        self.assertIn('Ahora puedes organizar tu panel según cómo trabajas: solo o con un equipo. Conservas tus herramientas, datos y permisos.', html)
        self.assertNotIn('data-work-nav=', html)
        self.assertIsNone(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        notice = db.session.get(OwnerWorkUpdateNotice, self.audit.owner.id)
        self.assertEqual(notice.action, 'seen')
        self.assertIsNotNone(notice.seen_at)
        self.client.post('/logout')
        device = self.fresh_device()
        self.assertNotIn('data-work-update', device.get('/').get_data(as_text=True))
        self.assertEqual(OwnerWorkUpdateNotice.query.count(), 1)

    def test_later_persists_and_can_configure_from_business_afterwards(self):
        self.client.get('/')
        response = self.client.post('/settings/work-style/update', data={'action': 'later'})
        self.assertEqual((response.status_code, response.location), (303, '/'))
        self.assertEqual(db.session.get(OwnerWorkUpdateNotice, self.audit.owner.id).action, 'later')
        self.assertIsNone(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        self.client.post('/logout')
        device = self.fresh_device()
        html = device.get('/').get_data(as_text=True)
        self.assertNotIn('data-work-update', html)
        self.assertNotIn('data-work-nav=', html)
        self.assertIn('Forma de trabajo', device.get('/settings').get_data(as_text=True))
        device.post('/settings/work-style', data={'work_style': 'solo'})
        self.assertIn('data-work-nav="pos"', device.get('/').get_data(as_text=True))

    def test_personalize_opens_existing_assistant_without_changing_preferences(self):
        self.client.get('/')
        response = self.client.post('/settings/work-style/update', data={'action': 'personalize'})
        self.assertEqual(response.location, '/settings/work-style/setup')
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertEqual(html.count('data-work-style-form'), 1)
        self.assertIn('¿Cómo trabajas en tu negocio?', html)
        self.assertIsNone(db.session.get(OwnerWorkPreference, self.audit.owner.id))
        self.assertEqual(db.session.get(OwnerWorkUpdateNotice, self.audit.owner.id).action, 'personalize')
        self.client.post('/settings/work-style', data={'action': 'defer', 'source': 'onboarding'})
        html = self.fresh_device().get('/').get_data(as_text=True)
        self.assertNotIn('data-work-update', html)
        self.assertNotIn('data-work-style-form', html)
        self.assertNotIn('data-work-nav=', html)

    def test_excludes_staff_and_configured_or_postponed_owners(self):
        manager, _ = self.audit.fixture.add_member('MANAGER', 'update-manager@example.com')
        manager_client = self.audit.app.test_client()
        manager_client.post('/login', data={'email': manager.email, 'password': 'Password123'})
        for client in (self.audit.client, manager_client):
            self.assertNotIn('data-work-update', client.get('/', follow_redirects=True).get_data(as_text=True))
            self.assertEqual(client.post('/settings/work-style/update', data={'action': 'personalize'}).status_code, 403)
            self.assertEqual(client.get('/settings/work-style/setup').status_code, 403)
        self.assertEqual(OwnerWorkUpdateNotice.query.count(), 0)
        for mode in ('solo', 'team_operations', 'team_supervision', 'deferred', 'pending'):
            preference = db.session.get(OwnerWorkPreference, self.audit.owner.id)
            if preference is None:
                initialize_new_owner(self.audit.owner)
                db.session.flush()
                preference = db.session.get(OwnerWorkPreference, self.audit.owner.id)
            preference.mode = mode
            db.session.commit()
            self.assertNotIn('data-work-update', self.client.get('/').get_data(as_text=True))
        self.assertEqual(OwnerWorkUpdateNotice.query.count(), 0)

    def test_only_subscribed_existing_owners_and_not_public_or_other_pages(self):
        owner = self.audit.owner
        owner.manual_pro_access = False
        owner.stripe_subscription_id = 'sub_fictitious_update'
        owner.current_period_end = datetime.utcnow() + timedelta(days=20)
        owner.subscription_status = 'trialing'
        db.session.commit()
        self.assertNotIn('data-work-update', self.client.get('/').get_data(as_text=True))
        owner.subscription_status = 'active'
        db.session.commit()
        self.client.get('/settings')
        self.client.get('/sell')
        self.client.head('/')
        self.assertEqual(OwnerWorkUpdateNotice.query.count(), 0)
        self.assertIn('data-work-update', self.client.get('/').get_data(as_text=True))
        other, _ = self.audit.fixture.add_owner('other-update@example.com')
        other.manual_pro_access = True
        db.session.commit()
        other_client = self.audit.app.test_client()
        other_client.post('/login', data={'email': other.email, 'password': 'Password123'})
        self.assertIn('data-work-update', other_client.get('/').get_data(as_text=True))
        self.assertEqual(OwnerWorkUpdateNotice.query.count(), 2)

    def test_invalid_action_and_forged_owner_id_do_not_modify_another_owner(self):
        other, _ = self.audit.fixture.add_owner('unseen-update@example.com')
        self.client.post('/settings/work-style/update', data={'action': 'invalid'})
        self.assertEqual(OwnerWorkUpdateNotice.query.count(), 0)
        self.client.post('/settings/work-style/update', data={'action': 'later', 'user_id': other.id})
        self.assertIsNone(db.session.get(OwnerWorkUpdateNotice, other.id))
        self.assertEqual(db.session.get(OwnerWorkUpdateNotice, self.audit.owner.id).action, 'later')
