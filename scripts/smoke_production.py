"""Read-only production smoke test with explicit, isolated QA credentials."""

from __future__ import annotations

import http.cookiejar
import os
import re
import sys
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


PUBLIC_PATHS = ("/", "/register", "/login")
AUTHENTICATED_PATHS = (
    "/",
    "/sell",
    "/products",
    "/cash-register",
    "/customers",
    "/credit",
    "/reports",
    "/pro/hub",
    "/pro/purchases",
    "/subscription",
)
KNOWN_PERSONAL_OR_ADMIN_EMAILS = frozenset({"albertonicopat@gmail.com"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class SmokeConfigurationError(RuntimeError):
    """Raised before a smoke test could use unsafe credentials."""


@dataclass(frozen=True)
class QACredentials:
    email: str
    password: str


def _normalized_emails(value: str | None) -> set[str]:
    return {
        email.strip().casefold()
        for email in (value or "").split(",")
        if email.strip()
    }


def qa_credentials_from_environment(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> QACredentials | None:
    """Return explicitly confirmed QA credentials or skip authentication safely."""
    values = os.environ if environ is None else environ
    email = values.get("PATIA_QA_EMAIL", "").strip().casefold()
    password = values.get("PATIA_QA_PASSWORD", "")

    if not email and not password:
        return None
    if not email or not password:
        raise SmokeConfigurationError(
            "PATIA_QA_EMAIL y PATIA_QA_PASSWORD deben configurarse juntos."
        )
    if values.get("PATIA_QA_ACCOUNT_CONFIRMED", "").strip().casefold() not in TRUE_VALUES:
        raise SmokeConfigurationError(
            "Define PATIA_QA_ACCOUNT_CONFIRMED=true para confirmar que es una cuenta QA independiente."
        )

    forbidden = set(KNOWN_PERSONAL_OR_ADMIN_EMAILS)
    forbidden.update(_normalized_emails(values.get("PATIA_ADMIN_EMAIL")))
    forbidden.update(_normalized_emails(values.get("PATIA_SMOKE_FORBIDDEN_EMAILS")))
    if email in forbidden:
        raise SmokeConfigurationError(
            "La cuenta QA no puede ser una cuenta personal ni administrativa."
        )
    return QACredentials(email=email, password=password)


def _request(opener, base_url: str, path: str) -> tuple[int, str, str]:
    response = opener.open(
        Request(
            urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
            headers={"User-Agent": "PATIA-QA-Smoke/1.0"},
        ),
        timeout=30,
    )
    return response.status, response.geturl(), response.read().decode("utf-8")


def _login(opener, base_url: str, credentials: QACredentials) -> None:
    _, _, login_html = _request(opener, base_url, "/login")
    form_match = re.search(
        r'<form[^>]*class="[^"]*auth-form[^"]*"[^>]*>.*?</form>',
        login_html,
        re.DOTALL,
    )
    if not form_match:
        raise RuntimeError("No se encontró el formulario de login.")
    csrf_match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        form_match.group(0),
    )
    if not csrf_match:
        raise RuntimeError("No se encontró el token CSRF del login.")
    payload = urlencode(
        {
            "csrf_token": csrf_match.group(1),
            "email": credentials.email,
            "password": credentials.password,
        }
    ).encode()
    response = opener.open(
        Request(
            urljoin(base_url.rstrip("/") + "/", "login"),
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": urljoin(base_url.rstrip("/") + "/", "login"),
                "User-Agent": "PATIA-QA-Smoke/1.0",
            },
        ),
        timeout=30,
    )
    html = response.read().decode("utf-8")
    if "auth-v2__form" in html or "/login" in response.geturl():
        raise RuntimeError("La cuenta QA no pudo iniciar sesión.")


def run() -> int:
    base_url = os.environ.get("PATIA_QA_BASE_URL", "https://patiaapp.com").strip()
    try:
        credentials = qa_credentials_from_environment()
    except SmokeConfigurationError as exc:
        print(f"ERROR de configuración QA: {exc}", file=sys.stderr)
        return 2

    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    failures = 0
    try:
        for path in PUBLIC_PATHS:
            status, final_url, _ = _request(opener, base_url, path)
            print(f"PUBLIC {path}: {status} ({final_url})")
            failures += status >= 500

        if credentials is None:
            print(
                "SKIP autenticado: no existen credenciales PATIA_QA_* explícitas. "
                "No se utilizará ninguna cuenta personal."
            )
            return 1 if failures else 0

        _login(opener, base_url, credentials)
        print("LOGIN QA: OK")
        for path in AUTHENTICATED_PATHS:
            status, final_url, _ = _request(opener, base_url, path)
            redirected_to_login = "/login" in final_url
            print(
                f"AUTH {path}: {status} ({final_url})"
                + (" [LOGIN REDIRECT]" if redirected_to_login else "")
            )
            failures += status >= 500 or redirected_to_login
    except (HTTPError, URLError, RuntimeError) as exc:
        print(f"ERROR de smoke test: {exc}", file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
