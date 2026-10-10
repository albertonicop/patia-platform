"""Historical receipts survive reversals without adding sales to reports."""
from decimal import Decimal
import unittest

from app import db
from app.models import CashMovement, InventoryMovement, Product, ReversedSaleLine, Sale
from app.cash.services import expected_cash
from test_cashier_session import CashierFixture


class TicketReversalTests(unittest.TestCase):
    def setUp(self):
        self.audit = CashierFixture()
        self.addCleanup(self.audit.close)
        self.audit.client.post('/cash-register/open', data={'opening_cash': '100'})

    def test_full_return_preserves_original_lines_totals_and_actor_on_reprint(self):
        x = self.audit
        _, manager = x.fixture.add_member('MANAGER', 'returns.demo@example.com')
        manager_client = x.fixture.client_for(manager.user)
        second = Product(organization_id=x.member.organization_id, user_id=x.owner.id,
                         name='Pan demo', sku='DEMO-SECOND', sale_price=Decimal('7.50'),
                         cost_price=Decimal('2'), stock=20, min_stock=1)
        db.session.add(second)
        db.session.commit()
        response = x.sell(items=[{'product_id': x.product.id, 'quantity': 2},
                                 {'product_id': second.id, 'quantity': 1}])
        self.assertEqual(response.status_code, 200)
        ticket_url = response.json['ticket_url']
        lines = Sale.query.order_by(Sale.id).all()
        original = [(r.id, r.quantity, r.unit_price, r.total, r.created_at) for r in lines]
        for line, actor_client in zip(lines, [manager_client, x.owner_client]):
            self.assertEqual(actor_client.post(f'/sales/{line.id}/return').status_code, 302)
        self.assertEqual(Sale.query.count(), 0)
        archived = ReversedSaleLine.query.order_by(ReversedSaleLine.original_sale_id).all()
        self.assertEqual([(r.original_sale_id, r.quantity, r.unit_price, r.total, r.created_at)
                          for r in archived], original)
        for row, actor in zip(archived, [manager, x.owner_member]):
            self.assertEqual(row.performed_by_member_id, actor.id)
            self.assertEqual(row.sales_ticket.cashier_member_id, x.member.id)
            self.assertEqual(row.organization_id, x.member.organization_id)
        for model, kind in [(InventoryMovement, 'RETURN'), (CashMovement, 'REFUND')]:
            movements = model.query.filter_by(movement_type=kind).order_by(model.id).all()
            self.assertEqual([r.performed_by_member_id for r in movements], [manager.id, x.owner_member.id])
            self.assertTrue(all(r.organization_id == x.member.organization_id for r in movements))
        db.session.refresh(x.product)
        db.session.refresh(second)
        self.assertEqual((x.product.stock, second.stock), (30, 20))
        self.assertEqual(expected_cash(archived[0].sales_ticket.cash_register_session_id), Decimal('100'))
        pos = x.client.get('/sell').get_data(as_text=True)
        self.assertIn('Ticket devuelto completamente', pos)
        self.assertIn(ticket_url + '?print=1', pos)
        owner_pos = x.owner_client.get('/sell').get_data(as_text=True)
        for sale_id, *_ in original:
            self.assertNotIn(f'action="/sales/{sale_id}/return"', owner_pos)
        x.product.name = 'Nombre cambiado después'
        x.product.sale_price = Decimal('999')
        second.sale_price = Decimal('999')
        db.session.commit()
        for path in [ticket_url, ticket_url + '?print=1', f'/ticket/sale-{original[0][0]}', f'/receipt/{original[0][0]}']:
            printed = x.client.get(path, follow_redirects=True)
            self.assertEqual(printed.status_code, 200)
            html = printed.get_data(as_text=True)
            for text in ('Ticket devuelto completamente', 'Rol de canela demo', 'Pan demo', '28.00', '10.25', '7.50', 'Lucia Caja Demo'):
                self.assertIn(text, html)
            self.assertNotIn('Nombre cambiado después', html)
            self.assertNotIn('999.00', html)
        self.assertEqual(x.sell(request_id=response.json['ticket_id']).status_code, 409)
        self.assertEqual(x.owner_client.post('/cash-register/close', data={'counted_cash': '100'}).status_code, 302)
        for sale_id, *_ in original:
            for action in ('return', 'return', 'cancel'):
                self.assertEqual(x.owner_client.post(f'/sales/{sale_id}/{action}').status_code, 302)
        self.assertEqual(ReversedSaleLine.query.count(), 2)
        self.assertEqual(CashMovement.query.filter_by(movement_type='REFUND').count(), 2)
        self.assertEqual(InventoryMovement.query.filter_by(movement_type='RETURN').count(), 2)
        self.assertEqual(expected_cash(archived[0].sales_ticket.cash_register_session_id), Decimal('100'))

    def test_partial_return_keeps_full_original_receipt_and_net_sales(self):
        x = self.audit
        second = Product(organization_id=x.member.organization_id, user_id=x.owner.id,
                         name='Otro pan demo', sku='PARTIAL-DEMO', sale_price=Decimal('10.25'),
                         cost_price=Decimal('2'), stock=20, min_stock=1)
        db.session.add(second)
        db.session.commit()
        response = x.sell(items=[{'product_id': x.product.id, 'quantity': 1},
                                 {'product_id': second.id, 'quantity': 2}])
        self.assertEqual(response.status_code, 200)
        lines = Sale.query.order_by(Sale.id).all()
        self.assertEqual(x.owner_client.post(f'/sales/{lines[0].id}/return').status_code, 302)
        html = x.client.get(response.json['ticket_url']).get_data(as_text=True)
        self.assertIn('Ticket con devolución o cancelación parcial', html)
        self.assertIn('30.75', html)
        owner_pos = x.owner_client.get('/sell').get_data(as_text=True)
        self.assertNotIn(f'action="/sales/{lines[0].id}/return"', owner_pos)
        self.assertIn(f'action="/sales/{lines[1].id}/return"', owner_pos)
        self.assertEqual(Sale.query.one().total, Decimal('20.50'))
        self.assertEqual(ReversedSaleLine.query.one().total, Decimal('10.25'))

    def test_card_cancellation_is_reprintable_and_does_not_refund_cash(self):
        x = self.audit
        response = x.sell(payment_method='card', amount_received=None)
        sale_id = Sale.query.one().id
        self.assertEqual(x.owner_client.post(f'/sales/{sale_id}/cancel').status_code, 302)
        html = x.client.get(response.json['ticket_url']).get_data(as_text=True)
        self.assertIn('Ticket cancelado', html)
        self.assertIn('20.50', html)
        self.assertEqual(CashMovement.query.filter_by(movement_type='REFUND').count(), 0)
        self.assertEqual(x.owner_client.post(f'/sales/{sale_id}/return').status_code, 302)
        self.assertEqual(ReversedSaleLine.query.count(), 1)

    def test_archived_ticket_and_retry_remain_isolated_and_permission_checked(self):
        x = self.audit
        response = x.sell()
        sale_id = Sale.query.one().id
        x.owner_client.post(f'/sales/{sale_id}/return')
        self.assertEqual(x.client.post(f'/sales/{sale_id}/return').status_code, 403)
        foreign, foreign_member = x.fixture.add_owner('foreign-return@example.com')
        foreign_client = x.fixture.client_for(foreign)
        with foreign_client.session_transaction() as session:
            session['organization_id'] = foreign_member.organization_id
        self.assertEqual(foreign_client.get(response.json['ticket_url']).status_code, 404)
        self.assertEqual(foreign_client.get(f'/ticket/sale-{sale_id}').status_code, 404)
        self.assertEqual(foreign_client.get(f'/receipt/{sale_id}').status_code, 404)
        self.assertEqual(foreign_client.post(f'/sales/{sale_id}/return').status_code, 404)
