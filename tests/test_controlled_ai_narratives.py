import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


os.environ.setdefault("SECRET_KEY", "controlled-ai-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_ai")
os.environ.setdefault("STRIPE_PRICE_ID", "price_ai")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_ai")
os.environ.setdefault("PUBLIC_BASE_URL", "https://ai.test")

from app import create_app, db
from app.ai_narratives import controlled_narrative
from app.models import AiNarrativeRun, User
from app.team.services import ensure_owner_organization


class ControlledAiNarrativeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-ai-")
        database_path = Path(self.temp_dir.name, "ai.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = (
            f"sqlite:///{database_path.as_posix()}"
        )
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            PATIA_AI_ENABLED=True,
            OPENAI_API_KEY="test-key-not-real",
            PATIA_AI_MODEL="gpt-5-mini",
            PATIA_AI_INPUT_USD_PER_MILLION="0.25",
            PATIA_AI_OUTPUT_USD_PER_MILLION="2.00",
            PATIA_AI_GLOBAL_MONTHLY_USD="10",
            PATIA_AI_ORGANIZATION_MONTHLY_USD="1",
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        owner = User(
            email="owner-ai@example.com",
            company_name="AI Test",
            email_verified=True,
            manual_pro_access=True,
        )
        owner.set_password("password123")
        db.session.add(owner)
        db.session.flush()
        self.organization = ensure_owner_organization(owner).organization
        db.session.commit()
        self.fallback = {
            "summary": "Resumen verificable",
            "what_happened": "Las ventas cambiaron",
            "why_it_matters": "Conviene revisar el resultado",
            "recommended_actions": ["Revisar Reportes"],
            "limitations": [],
            "data_period": "2026-07",
        }
        self.metrics = {
            "sales_change": "10",
            "ticket_count": 8,
            "low_stock_count": 2,
        }

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def response(self, **overrides):
        value = {
            "summary": "Las ventas crecieron 10 por ciento.",
            "what_happened": "Se registraron 8 ventas.",
            "why_it_matters": "El avance mejora el resultado.",
            "recommended_actions": ["Revisar los 2 productos con poco stock."],
            "limitations": [],
            "data_period": "2026-07",
        }
        value.update(overrides)
        return {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(value),
                        }
                    ]
                }
            ],
            "usage": {"input_tokens": 400, "output_tokens": 120},
        }

    def generate(self):
        return controlled_narrative(
            organization_id=self.organization.id,
            feature="pulse",
            language="es",
            period="2026-07",
            metrics=self.metrics,
            fallback=self.fallback,
        )

    @patch("app.ai_narratives._post_response")
    def test_valid_response_is_audited_and_cached(self, post_response):
        post_response.return_value = self.response()
        output, source = self.generate()
        db.session.commit()
        self.assertEqual(source, "ai")
        self.assertIn("10", output["summary"])
        record = AiNarrativeRun.query.one()
        self.assertEqual(record.input_tokens, 400)
        self.assertEqual(record.output_tokens, 120)
        self.assertGreater(record.estimated_cost_microusd, 0)
        cached, cached_source = self.generate()
        self.assertEqual(cached_source, "cache")
        self.assertEqual(cached, output)
        self.assertEqual(post_response.call_count, 1)

    @patch("app.ai_narratives._post_response")
    def test_unverified_number_uses_fallback(self, post_response):
        post_response.return_value = self.response(
            summary="Las ventas crecieron 999 por ciento."
        )
        output, source = self.generate()
        db.session.commit()
        self.assertEqual(source, "fallback")
        self.assertEqual(output, self.fallback)
        self.assertEqual(
            AiNarrativeRun.query.one().error_code,
            "unverified_number",
        )

    @patch("app.ai_narratives._post_response")
    def test_invalid_json_and_missing_fields_use_fallback(
        self, post_response
    ):
        post_response.return_value = {
            "output": [
                {"content": [{"type": "output_text", "text": "not-json"}]}
            ],
            "usage": {},
        }
        self.assertEqual(self.generate()[1], "fallback")
        db.session.rollback()
        post_response.return_value = self.response(summary=None)
        self.assertEqual(self.generate()[1], "fallback")

    def test_disabled_or_missing_key_never_breaks_patia(self):
        self.app.config["PATIA_AI_ENABLED"] = False
        output, source = self.generate()
        self.assertEqual((output, source), (self.fallback, "fallback"))
        self.assertEqual(AiNarrativeRun.query.count(), 0)
        self.app.config["PATIA_AI_ENABLED"] = True
        self.app.config["OPENAI_API_KEY"] = ""
        output, source = self.generate()
        self.assertEqual((output, source), (self.fallback, "fallback"))

    @patch("app.ai_narratives._post_response")
    def test_daily_limit_prevents_reload_calls_when_data_changes(
        self, post_response
    ):
        post_response.return_value = self.response()
        self.generate()
        db.session.commit()
        self.metrics["ticket_count"] = 9
        output, source = self.generate()
        self.assertEqual((output, source), (self.fallback, "daily_limit"))
        self.assertEqual(post_response.call_count, 1)

    @patch("app.ai_narratives._post_response")
    def test_failed_pulse_is_not_retried_on_reload(self, post_response):
        post_response.return_value = {
            "output": [
                {"content": [{"type": "output_text", "text": "not-json"}]}
            ],
            "usage": {},
        }
        self.assertEqual(self.generate()[1], "fallback")
        db.session.commit()

        output, source = self.generate()

        self.assertEqual((output, source), (self.fallback, "daily_limit"))
        self.assertEqual(post_response.call_count, 1)

    def test_sensitive_payload_is_rejected_before_network(self):
        with self.assertRaisesRegex(Exception, "sensitive_payload"):
            controlled_narrative(
                organization_id=self.organization.id,
                feature="pulse",
                language="es",
                period="2026-07",
                metrics={"customer_email": "private@example.com"},
                fallback=self.fallback,
            )

    @patch("app.ai_narratives._post_response")
    def test_timeout_rate_limit_and_server_error_fall_back(
        self, post_response
    ):
        for error in (
            TimeoutError(),
            HTTPError("https://api.openai.com", 429, "limited", {}, None),
            HTTPError("https://api.openai.com", 500, "server", {}, None),
        ):
            post_response.side_effect = [error, error]
            output, source = self.generate()
            self.assertEqual((output, source), (self.fallback, "fallback"))
            db.session.rollback()
            post_response.reset_mock(side_effect=True)

    @patch("app.ai_narratives._post_response")
    def test_prompt_contains_only_aggregated_data_and_language_isolated(
        self, post_response
    ):
        post_response.return_value = self.response()
        self.generate()
        request_payload = post_response.call_args.args[1]
        serialized = json.dumps(request_payload)
        self.assertNotIn("owner-ai@example.com", serialized)
        self.assertNotIn("password", serialized.lower())
        db.session.commit()
        english_response = self.response(
            summary="Sales increased 10 percent.",
            what_happened="There were 8 recorded sales.",
            why_it_matters="The result is improving.",
            recommended_actions=["Review the 2 low-stock products."],
        )
        post_response.return_value = english_response
        _, source = controlled_narrative(
            organization_id=self.organization.id,
            feature="pulse",
            language="en",
            period="2026-07",
            metrics=self.metrics,
            fallback={**self.fallback, "summary": "Verified summary"},
        )
        self.assertEqual(source, "ai")

    @patch("app.ai_narratives._post_response")
    def test_organization_cache_is_isolated(self, post_response):
        post_response.return_value = self.response()
        self.generate()
        db.session.commit()
        other = User(
            email="other-ai@example.com",
            company_name="Other AI",
            email_verified=True,
            manual_pro_access=True,
        )
        other.set_password("password123")
        db.session.add(other)
        db.session.flush()
        other_org = ensure_owner_organization(other).organization
        db.session.commit()
        _, source = controlled_narrative(
            organization_id=other_org.id,
            feature="pulse",
            language="es",
            period="2026-07",
            metrics=self.metrics,
            fallback=self.fallback,
        )
        self.assertEqual(source, "ai")
        self.assertEqual(post_response.call_count, 2)


if __name__ == "__main__":
    unittest.main()
