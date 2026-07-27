import os
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event as sqlalchemy_event


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "pro-dashboard-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_pro_dashboard")
os.environ.setdefault("STRIPE_PRICE_ID", "price_test_pro_dashboard")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_pro_dashboard")
os.environ.setdefault("PUBLIC_BASE_URL", "https://pro-dashboard.test")

from app import create_app, db
from app.models import (
    CashMovement,
    CashRegisterSession,
    Customer,
    CustomerCreditMovement,
    InventoryMovement,
    OrganizationMember,
    Product,
    Sale,
    User,
)
from app.pro.services import build_executive_dashboard
from app.team.services import ensure_owner_organization


class ProExecutiveDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="patia-pro-dashboard-"
        )
        database_path = Path(self.temp_dir.name, "pro-dashboard.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner, self.membership = self._owner(
            "owner@pro-dashboard.test",
            manual_pro=True,
        )
        self.product = Product(
            organization_id=self.membership.organization_id,
            user_id=self.owner.id,
            sku="PRO-001",
            name="Producto ejecutivo",
            category="General",
            cost_price=Decimal("60.00"),
            sale_price=Decimal("100.00"),
            stock=20,
            min_stock=2,
        )
        db.session.add(self.product)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def _owner(self, email, *, manual_pro=False, trial_plan="STARTER"):
        user = User(
            email=email,
            company_name=f"Negocio {email}",
            email_verified=True,
            manual_pro_access=manual_pro,
            trial_plan_code=trial_plan,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def _member(self, role):
        user = User(
            email=f"{role.lower()}@pro-dashboard.test",
            company_name="Equipo ejecutivo",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = OrganizationMember(
            organization_id=self.membership.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(membership)
        db.session.commit()
        return user, membership

    def _client(self, user, membership, *, language="es"):
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = membership.organization_id
            flask_session["language"] = language
        return client

    def _sale(self, *, total, cost, created_at, ticket_id):
        sale = Sale(
            organization_id=self.membership.organization_id,
            user_id=self.owner.id,
            product_id=self.product.id,
            quantity=1,
            unit_price=Decimal(str(total)),
            total=Decimal(str(total)),
            unit_cost=(
                None if cost is None else Decimal(str(cost))
            ),
            created_at=created_at,
            ticket_id=ticket_id,
            payment_method="cash",
        )
        db.session.add(sale)
        db.session.commit()
        return sale

    def test_starter_sees_pro_preview_without_executive_data(self):
        starter, membership = self._owner("starter@pro-dashboard.test")
        client = self._client(starter, membership)

        response = client.get("/pro")
        self.assertEqual(response.status_code, 200)
        preview = response.get_data(as_text=True)
        self.assertIn("Panel ejecutivo", preview)
        self.assertIn("Actualizar a Pro", preview)
        self.assertNotIn("executiveAnalytics", preview)

        dashboard = client.get("/")
        html = dashboard.get_data(as_text=True)
        self.assertIn("Panel ejecutivo", html)
        self.assertIn(">Pro</small>", html)
        self.assertGreaterEqual(
            html.count('class="sidebar-v2__section-label"'), 3
        )
        self.assertIn('href="/team"', html)
        self.assertNotIn("executiveAnalytics", html)

    def test_pro_owner_and_pro_trial_can_open_dashboard(self):
        response = self._client(self.owner, self.membership).get("/pro")
        self.assertEqual(response.status_code, 200)
        self.assertIn("PATIA PRO", response.get_data(as_text=True))
        self.assertIn("Panel ejecutivo", response.get_data(as_text=True))

        trial, membership = self._owner(
            "pro-trial@pro-dashboard.test",
            trial_plan="PRO",
        )
        trial_response = self._client(trial, membership).get("/pro")
        self.assertEqual(trial_response.status_code, 200)

    def test_manager_can_view_cashier_cannot_and_only_owner_changes_goal(self):
        manager, manager_membership = self._member("MANAGER")
        cashier, cashier_membership = self._member("CASHIER")

        manager_client = self._client(manager, manager_membership)
        self.assertEqual(manager_client.get("/pro").status_code, 200)
        self.assertEqual(
            manager_client.post(
                "/pro/monthly-goal",
                data={"monthly_sales_goal": "50000"},
            ).status_code,
            403,
        )
        cashier_client = self._client(cashier, cashier_membership)
        self.assertEqual(cashier_client.get("/pro").status_code, 403)

        owner_client = self._client(self.owner, self.membership)
        goal = owner_client.post(
            "/pro/monthly-goal",
            data={"monthly_sales_goal": "50000.25"},
        )
        self.assertEqual(goal.status_code, 302)
        db.session.refresh(self.membership.organization)
        self.assertEqual(
            self.membership.organization.monthly_sales_goal,
            Decimal("50000.25"),
        )
        owner_client.post(
            "/pro/monthly-goal",
            data={"monthly_sales_goal": ""},
        )
        db.session.refresh(self.membership.organization)
        self.assertIsNone(self.membership.organization.monthly_sales_goal)

    def test_kpis_comparison_and_profit_coverage_use_historical_cost(self):
        now = datetime.utcnow()
        self._sale(
            total="100.00",
            cost="60.00",
            created_at=now - timedelta(days=1),
            ticket_id="current-known",
        )
        self._sale(
            total="50.00",
            cost=None,
            created_at=now - timedelta(days=2),
            ticket_id="current-unknown",
        )
        self._sale(
            total="75.00",
            cost="50.00",
            created_at=now - timedelta(days=8),
            ticket_id="previous",
        )

        data = build_executive_dashboard(
            self.membership.organization,
            {"period": "7d"},
        )
        kpis = data["executive_kpis"]
        self.assertEqual(kpis["sales"], Decimal("150.00"))
        self.assertEqual(kpis["profit"], Decimal("40.00"))
        self.assertEqual(kpis["margin"], Decimal("40.0"))
        self.assertEqual(kpis["average_ticket"], Decimal("75.00"))
        self.assertEqual(kpis["ticket_count"], 2)
        self.assertEqual(kpis["profit_coverage"], Decimal("66.7"))
        self.assertEqual(kpis["sales_change"], Decimal("100.0"))

    def test_projection_requires_multiple_days_and_sales(self):
        now = datetime.utcnow()
        for index in range(3):
            self._sale(
                total="100.00",
                cost="50.00",
                created_at=now - timedelta(days=index),
                ticket_id=f"projection-{index}",
            )
        data = build_executive_dashboard(
            self.membership.organization,
            {"period": "this_month"},
        )
        self.assertIsNotNone(data["monthly_projection"])

        Sale.query.delete()
        db.session.commit()
        data = build_executive_dashboard(
            self.membership.organization,
            {"period": "this_month"},
        )
        self.assertIsNone(data["monthly_projection"])

    def test_organization_isolation_excludes_other_business_sales(self):
        other_owner, other_membership = self._owner(
            "other@pro-dashboard.test",
            manual_pro=True,
        )
        other_product = Product(
            organization_id=other_membership.organization_id,
            user_id=other_owner.id,
            sku="OTHER-1",
            name="Producto ajeno",
            category="General",
            cost_price=Decimal("1.00"),
            sale_price=Decimal("9999.00"),
            stock=1,
            min_stock=0,
        )
        db.session.add(other_product)
        db.session.flush()
        db.session.add(
            Sale(
                organization_id=other_membership.organization_id,
                user_id=other_owner.id,
                product_id=other_product.id,
                quantity=1,
                unit_price=Decimal("9999.00"),
                total=Decimal("9999.00"),
                unit_cost=Decimal("1.00"),
                created_at=datetime.utcnow(),
                ticket_id="other-ticket",
            )
        )
        db.session.commit()

        response = self._client(self.owner, self.membership).get(
            "/pro?period=7d"
        )
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("9,999", html)
        self.assertNotIn("Producto ajeno", html)

    def test_starter_report_does_not_serialize_profit_but_pro_does(self):
        self._sale(
            total="100.00",
            cost="60.00",
            created_at=datetime.utcnow(),
            ticket_id="visibility",
        )
        pro_html = self._client(self.owner, self.membership).get(
            "/reports"
        ).get_data(as_text=True)
        pro_script = pro_html.split("window.reportAnalytics", 1)[1]
        self.assertIn('"profit"', pro_script)

        starter, starter_membership = self._owner(
            "starter-report@pro-dashboard.test"
        )
        starter_product = Product(
            organization_id=starter_membership.organization_id,
            user_id=starter.id,
            sku="STARTER-R",
            name="Starter report",
            category="General",
            cost_price=Decimal("10.00"),
            sale_price=Decimal("20.00"),
            stock=1,
            min_stock=0,
        )
        db.session.add(starter_product)
        db.session.flush()
        db.session.add(
            Sale(
                organization_id=starter_membership.organization_id,
                user_id=starter.id,
                product_id=starter_product.id,
                quantity=1,
                unit_price=Decimal("20.00"),
                total=Decimal("20.00"),
                unit_cost=Decimal("10.00"),
                created_at=datetime.utcnow(),
                ticket_id="starter-visibility",
            )
        )
        db.session.commit()
        starter_html = self._client(
            starter,
            starter_membership,
        ).get("/reports").get_data(as_text=True)
        starter_script = starter_html.split("window.reportAnalytics", 1)[1]
        self.assertNotIn('"profit"', starter_script)

    def test_executive_copy_is_translated_to_english(self):
        response = self._client(
            self.owner,
            self.membership,
            language="en",
        ).get("/pro")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Executive Dashboard", html)
        self.assertIn("Monthly goal", html)
        self.assertNotIn("La perspectiva completa de tu negocio", html)

    def test_actionable_sections_are_backed_by_current_organization_data(self):
        now = datetime.utcnow()
        self.product.stock = 1
        self.product.min_stock = 4
        for index in range(5):
            product = Product(
                organization_id=self.membership.organization_id,
                user_id=self.owner.id,
                sku=f"DRIVER-{index}",
                name=f"Producto rentable {index}",
                category="General",
                cost_price=Decimal("10.00"),
                sale_price=Decimal(str(30 + index)),
                stock=20,
                min_stock=2,
            )
            db.session.add(product)
            db.session.flush()
            db.session.add(
                Sale(
                    organization_id=self.membership.organization_id,
                    user_id=self.owner.id,
                    product_id=product.id,
                    quantity=index + 1,
                    unit_price=product.sale_price,
                    total=product.sale_price * (index + 1),
                    unit_cost=product.cost_price,
                    created_at=now - timedelta(hours=index),
                    ticket_id=f"driver-{index}",
                    payment_method="cash",
                )
            )
        customer = Customer(
            organization_id=self.membership.organization_id,
            created_by_member_id=self.membership.id,
            name="Cliente con saldo",
            credit_enabled=True,
            credit_limit=Decimal("1000.00"),
        )
        db.session.add(customer)
        db.session.flush()
        db.session.add(
            CustomerCreditMovement(
                organization_id=self.membership.organization_id,
                customer_id=customer.id,
                performed_by_member_id=self.membership.id,
                movement_type="CHARGE",
                amount=Decimal("250.00"),
                balance_before=Decimal("0.00"),
                balance_after=Decimal("250.00"),
                request_id="pro-actionable-credit",
                created_at=now,
            )
        )
        db.session.commit()

        data = build_executive_dashboard(
            self.membership.organization,
            {"period": "7d"},
        )

        self.assertLessEqual(len(data["executive_drivers"]), 5)
        self.assertTrue(
            any(
                item["kind"] == "product"
                for item in data["executive_drivers"]
            )
        )
        self.assertTrue(
            any(
                item["kind"] == "day"
                for item in data["executive_drivers"]
            )
        )
        self.assertTrue(
            any(
                item["kind"] == "hour"
                for item in data["executive_drivers"]
            )
        )
        self.assertLessEqual(len(data["executive_attention"]), 4)
        self.assertIn(
            "stock",
            {item["key"] for item in data["executive_attention"]},
        )
        self.assertEqual(
            data["executive_control"]["inventory"]["low_stock"], 1
        )
        self.assertEqual(
            data["executive_control"]["inventory"]["suggested_units"], 3
        )
        self.assertEqual(
            data["executive_control"]["credit"]["balance"],
            Decimal("250.00"),
        )
        self.assertLessEqual(len(data["priority_actions"]), 3)
        self.assertTrue(
            all(item["url"].startswith("/") for item in data["priority_actions"])
        )

    def test_team_activity_is_hidden_for_one_person_and_uses_real_events(self):
        now = datetime.utcnow()
        without_team = build_executive_dashboard(
            self.membership.organization,
            {"period": "7d"},
        )
        self.assertFalse(without_team["team_activity"]["visible"])

        self._member("MANAGER")
        session = CashRegisterSession(
            organization_id=self.membership.organization_id,
            register_key="MAIN",
            status="CLOSED",
            opened_by_member_id=self.membership.id,
            closed_by_member_id=self.membership.id,
            opening_cash=Decimal("0.00"),
            expected_cash_at_close=Decimal("100.00"),
            counted_cash=Decimal("95.00"),
            difference=Decimal("-5.00"),
            opened_at=now - timedelta(hours=2),
            closed_at=now - timedelta(hours=1),
        )
        db.session.add(session)
        db.session.flush()
        db.session.add_all(
            [
                CashMovement(
                    organization_id=self.membership.organization_id,
                    cash_register_session_id=session.id,
                    performed_by_member_id=self.membership.id,
                    movement_type="WITHDRAWAL",
                    amount=Decimal("20.00"),
                    created_at=now - timedelta(hours=1),
                ),
                InventoryMovement(
                    organization_id=self.membership.organization_id,
                    product_id=self.product.id,
                    performed_by_member_id=self.membership.id,
                    movement_type="SALE_CANCELLATION",
                    quantity_delta=1,
                    stock_before=19,
                    stock_after=20,
                    reason="Prueba ejecutiva",
                    product_name=self.product.name,
                    product_sku=self.product.sku,
                    created_at=now,
                ),
                InventoryMovement(
                    organization_id=self.membership.organization_id,
                    product_id=self.product.id,
                    performed_by_member_id=self.membership.id,
                    movement_type="PHYSICAL_COUNT",
                    quantity_delta=1,
                    stock_before=19,
                    stock_after=20,
                    reason="Conteo de prueba",
                    product_name=self.product.name,
                    product_sku=self.product.sku,
                    created_at=now,
                ),
            ]
        )
        db.session.commit()

        data = build_executive_dashboard(
            self.membership.organization,
            {"period": "7d"},
        )
        activity = {
            item["key"]: item for item in data["team_activity"]["items"]
        }
        self.assertTrue(data["team_activity"]["visible"])
        self.assertEqual(activity["cancellations"]["count"], 1)
        self.assertEqual(activity["corrections"]["count"], 1)
        self.assertEqual(activity["withdrawals"]["count"], 1)
        self.assertEqual(
            activity["withdrawals"]["amount"], Decimal("20.00")
        )
        self.assertEqual(activity["differences"]["count"], 1)
        self.assertEqual(
            activity["differences"]["amount"], Decimal("5.00")
        )

    def test_sprint_1b_empty_states_render_without_team_or_sales(self):
        Product.query.delete()
        db.session.commit()

        response = self._client(self.owner, self.membership).get("/pro")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Aquí aparecerán los principales impulsores", html)
        self.assertIn(
            "No detectamos situaciones urgentes en este periodo", html
        )
        self.assertNotIn("Actividad del equipo", html)
        self.assertIn("Control del negocio", html)
        self.assertIn("Acciones prioritarias", html)

    def test_sprint_1b_excludes_other_organization_controls_and_alerts(self):
        other_owner, other_membership = self._owner(
            "other-actions@pro-dashboard.test",
            manual_pro=True,
        )
        other_product = Product(
            organization_id=other_membership.organization_id,
            user_id=other_owner.id,
            sku="OTHER-LOW",
            name="Alerta ajena",
            category="General",
            cost_price=Decimal("50.00"),
            sale_price=Decimal("100.00"),
            stock=0,
            min_stock=20,
        )
        other_customer = Customer(
            organization_id=other_membership.organization_id,
            created_by_member_id=other_membership.id,
            name="Cliente ajeno",
            credit_enabled=True,
            credit_limit=Decimal("50000.00"),
        )
        db.session.add_all((other_product, other_customer))
        db.session.flush()
        db.session.add(
            CustomerCreditMovement(
                organization_id=other_membership.organization_id,
                customer_id=other_customer.id,
                performed_by_member_id=other_membership.id,
                movement_type="CHARGE",
                amount=Decimal("9999.00"),
                balance_before=Decimal("0.00"),
                balance_after=Decimal("9999.00"),
                request_id="other-org-credit",
            )
        )
        db.session.commit()

        data = build_executive_dashboard(
            self.membership.organization,
            {"period": "7d"},
        )
        self.assertEqual(
            data["executive_control"]["credit"]["balance"],
            Decimal("0.00"),
        )
        self.assertEqual(
            data["executive_control"]["inventory"]["low_stock"], 0
        )
        response = self._client(self.owner, self.membership).get("/pro")
        html = response.get_data(as_text=True)
        self.assertNotIn("Alerta ajena", html)
        self.assertNotIn("9,999", html)

    def test_sprint_1b_copy_is_translated_to_english(self):
        Product.query.delete()
        db.session.commit()
        response = self._client(
            self.owner,
            self.membership,
            language="en",
        ).get("/pro")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("What drives the result", html)
        self.assertIn("What needs attention", html)
        self.assertIn("Business control", html)
        self.assertIn("Priority actions", html)
        self.assertNotIn("Qué impulsa el resultado", html)

    def test_sprint_1b_uses_a_bounded_number_of_queries(self):
        for index in range(25):
            db.session.add(
                Product(
                    organization_id=self.membership.organization_id,
                    user_id=self.owner.id,
                    sku=f"BOUND-{index}",
                    name=f"Producto {index}",
                    category="General",
                    cost_price=Decimal("10.00"),
                    sale_price=Decimal("20.00"),
                    stock=10,
                    min_stock=2,
                )
            )
        db.session.commit()
        statements = []

        def count_statement(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        sqlalchemy_event.listen(
            db.engine, "before_cursor_execute", count_statement
        )
        try:
            build_executive_dashboard(
                self.membership.organization,
                {"period": "7d"},
            )
        finally:
            sqlalchemy_event.remove(
                db.engine, "before_cursor_execute", count_statement
            )

        self.assertLessEqual(
            len(statements),
            30,
            f"Executive dashboard issued {len(statements)} SELECTs",
        )


if __name__ == "__main__":
    unittest.main()
