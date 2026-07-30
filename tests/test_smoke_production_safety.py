import unittest
from unittest.mock import patch

from scripts.smoke_production import (
    PUBLIC_PATHS,
    SmokeConfigurationError,
    qa_credentials_from_environment,
    run,
)


class ProductionSmokeSafetyTests(unittest.TestCase):
    def test_missing_qa_credentials_skips_authenticated_smoke(self):
        self.assertIsNone(qa_credentials_from_environment({}))

    @patch("scripts.smoke_production._login")
    @patch(
        "scripts.smoke_production._request",
        return_value=(200, "https://patiaapp.com/", ""),
    )
    @patch(
        "scripts.smoke_production.qa_credentials_from_environment",
        return_value=None,
    )
    def test_run_without_qa_credentials_never_attempts_login(
        self,
        _credentials,
        request_mock,
        login_mock,
    ):
        self.assertEqual(run(), 0)
        self.assertEqual(request_mock.call_count, len(PUBLIC_PATHS))
        login_mock.assert_not_called()

    def test_partial_credentials_are_rejected(self):
        with self.assertRaisesRegex(
            SmokeConfigurationError,
            "deben configurarse juntos",
        ):
            qa_credentials_from_environment({"PATIA_QA_EMAIL": "qa@example.com"})

    def test_unconfirmed_account_is_rejected(self):
        with self.assertRaisesRegex(
            SmokeConfigurationError,
            "PATIA_QA_ACCOUNT_CONFIRMED",
        ):
            qa_credentials_from_environment(
                {
                    "PATIA_QA_EMAIL": "qa@example.com",
                    "PATIA_QA_PASSWORD": "not-logged",
                }
            )

    def test_known_personal_admin_account_is_rejected(self):
        with self.assertRaisesRegex(
            SmokeConfigurationError,
            "personal ni administrativa",
        ):
            qa_credentials_from_environment(
                {
                    "PATIA_QA_EMAIL": "albertonicopat@gmail.com",
                    "PATIA_QA_PASSWORD": "not-logged",
                    "PATIA_QA_ACCOUNT_CONFIRMED": "true",
                }
            )

    def test_configured_admin_and_personal_accounts_are_rejected(self):
        base = {
            "PATIA_QA_PASSWORD": "not-logged",
            "PATIA_QA_ACCOUNT_CONFIRMED": "true",
            "PATIA_ADMIN_EMAIL": "admin@example.com",
            "PATIA_SMOKE_FORBIDDEN_EMAILS": "owner@example.com,active@example.com",
        }
        for email in ("admin@example.com", "OWNER@example.com", "active@example.com"):
            with self.subTest(email=email), self.assertRaises(
                SmokeConfigurationError
            ):
                qa_credentials_from_environment({**base, "PATIA_QA_EMAIL": email})

    def test_explicit_independent_qa_account_is_accepted(self):
        credentials = qa_credentials_from_environment(
            {
                "PATIA_QA_EMAIL": "qa-smoke@example.com",
                "PATIA_QA_PASSWORD": "not-logged",
                "PATIA_QA_ACCOUNT_CONFIRMED": "yes",
                "PATIA_ADMIN_EMAIL": "admin@example.com",
            }
        )

        self.assertEqual(credentials.email, "qa-smoke@example.com")
        self.assertEqual(credentials.password, "not-logged")


if __name__ == "__main__":
    unittest.main()
