#!/usr/bin/env python3
"""Read-only deployment checks, plus optional login; never invoke AI or create data.

Usage: python deploy/verify.py --base-url http://127.0.0.1
       python deploy/verify.py --credentials-file /private/login.json
       python deploy/verify.py --expected-cad-provider codex --expected-cad-model gpt-6-astra
Credentials JSON: {"email": "…", "password": "…"}. Output contains only
check names, status codes and fixed error codes, never response bodies or tokens.
Expected CAD values may also use JOYNIU_EXPECTED_CAD_PROVIDER and
JOYNIU_EXPECTED_CAD_MODEL. Codex binary configuration is checked; CLI login
remains explicitly unverified. No CLI authentication files or AI are accessed.
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    # Do not forward credentials to a redirected endpoint. A deployment with
    # an HTTP-to-HTTPS redirect should be checked using its final HTTPS URL.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets: list[tuple[str, str]] = []
        self.has_root = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.has_root |= values.get("id") == "root"
        if tag == "script" and values.get("src"):
            self.assets.append(("js", values["src"]))
        if tag == "link" and values.get("href"):
            rel = (values.get("rel") or "").lower().split()
            if "stylesheet" in rel or ("preload" in rel and values.get("as") == "style"):
                self.assets.append(("css", values["href"]))
            elif "modulepreload" in rel or ("preload" in rel and values.get("as") == "script"):
                self.assets.append(("js", values["href"]))


class Verifier:
    def __init__(self, base_url: str, timeout: float, *, expected_cad_provider=None, expected_cad_model=None,
                 expected_cad_workspaces=False):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())
        self.checks: list[dict] = []
        self.unverified_checks: list[dict] = []
        self.expected_cad_provider = expected_cad_provider
        self.expected_cad_model = expected_cad_model
        self.expected_cad_workspaces = expected_cad_workspaces

    def result(self, name, ok, status=None, error=None):
        item = {"name": name, "ok": bool(ok)}
        if status is not None:
            item["httpStatus"] = status
        if error:
            item["error"] = error
        self.checks.append(item)

    def fetch(self, name, path, *, expected=200, payload=None, token=None):
        headers = {"Accept": "*/*", "User-Agent": "JoyNiu-Deployment-Verify/1"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(urljoin(self.base_url, path), data=body, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                content_type = response.headers.get_content_type()
                if status != expected:
                    self.result(name, False, status, "unexpected_http_status")
                    return None
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    self.result(name, False, status, "response_too_large")
                    return None
                return status, content_type, raw
        except (URLError, OSError, ValueError, TimeoutError):
            self.result(name, False, error="request_failed")
            return None

    def json_check(self, name, path, validate, **options):
        response = self.fetch(name, path, **options)
        if response is None:
            return None
        status, content_type, raw = response
        try:
            data = json.loads(raw)
            valid = content_type == "application/json" and isinstance(data, dict) and bool(validate(data))
        except (ValueError, TypeError, KeyError, AttributeError):
            data, valid = None, False
        self.result(name, valid, status, None if valid else "unexpected_json_fields")
        return data if valid else None

    def homepage(self):
        response = self.fetch("homepage", "")
        if response is None:
            return
        status, content_type, raw = response
        page = PageAssets()
        try:
            page.feed(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            self.result("homepage", False, status, "invalid_html")
            return
        valid = content_type == "text/html" and page.has_root
        self.result("homepage", valid, status, None if valid else "unexpected_html")
        if not valid:
            return
        assets = list(dict.fromkeys(page.assets))
        kinds = {kind for kind, _ in assets}
        valid_assets = {"js", "css"}.issubset(kinds) and len(assets) <= 128
        self.result("homepage_assets", valid_assets, error=None if valid_assets else "missing_or_excessive_assets")
        if not valid_assets:
            return
        origin = urlsplit(self.base_url)
        for index, (kind, path) in enumerate(assets, 1):
            name = f"asset_{kind}_{index}"
            url = urlsplit(urljoin(self.base_url, path))
            if (url.scheme, url.netloc) != (origin.scheme, origin.netloc) or url.username or url.password:
                self.result(name, False, error="asset_origin_mismatch")
                continue
            response = self.fetch(name, url.geturl())
            if response is None:
                continue
            status, content_type, raw = response
            types = {"text/css"} if kind == "css" else {"text/javascript", "application/javascript", "application/ecmascript", "text/ecmascript"}
            valid = bool(raw.strip()) and content_type in types and not raw.lstrip().lower().startswith((b"<!doctype html", b"<html"))
            self.result(name, valid, status, None if valid else "unexpected_asset_content")

    def cad_provider(self, data):
        """Check the selected CAD engine, not the unrelated general chat relay."""
        if "cadProvider" in data:
            selected = data["cadProvider"]
            if not isinstance(selected, dict):
                self.result("cad_provider_selection", False, error="cad_provider_status_unavailable")
                return
            mode = selected.get("mode")
        else:
            # Existing relay deployments omit cadProvider. Only their explicit
            # remote/local-fallback status establishes this legacy selection.
            selected = data
            legacy_mode = data.get("mode")
            mode = "relay" if isinstance(legacy_mode, str) and legacy_mode in {"remote", "local-fallback"} else None
        if mode == "remote":
            mode = "relay"
        known = isinstance(mode, str) and mode in {"relay", "codex"}
        matches = known and (self.expected_cad_provider is None or mode == self.expected_cad_provider)
        self.result("cad_provider_selection", matches, error=None if matches else
                    "cad_provider_mismatch" if known else "cad_provider_status_unavailable")
        configured = selected.get("configured") is True
        self.result("cad_provider_configuration", configured, error=None if configured else "cad_provider_not_configured")
        if self.expected_cad_model is not None:
            matches = selected.get("model") == self.expected_cad_model
            self.result("cad_provider_model", matches, error=None if matches else "cad_model_mismatch")
        if mode != "codex":
            return
        # Current Codex status.configured checks an executable binary and a
        # valid model identifier. It does not establish a working CLI login.
        binary = selected.get("binaryAvailable", selected.get("configured"))
        self.result("cad_binary", binary is True, error=None if binary is True else
                    "cad_binary_unavailable" if binary is False else "cad_binary_status_unavailable")
        self.unverified_checks.append({"name": "cad_cli_authentication", "status": "unknown",
                                       "reason": "cli_login_precheck_required"})

    def run(self, credentials=None):
        self.homepage()
        self.json_check("health", "api/v1/health", lambda data: data.get("status") == "ok"
                        and all(isinstance(data.get(key), dict) and data[key].get("available") is True
                                for key in ("geometry", "dwg", "pdf")))
        expected_cad = self.expected_cad_provider is not None or self.expected_cad_model is not None
        ai_status = self.json_check("ai_configuration", "api/v1/ai/status", lambda data:
                        data.get("anonymousAllowed") is False and (expected_cad or data.get("configured") is True))
        if expected_cad and ai_status is not None:
            self.cad_provider(ai_status)
        self.json_check("cad_capabilities", "api/v1/cad-agent/capabilities", lambda data:
                        data.get("version") == "cad-agent-v1" and isinstance(data.get("formats"), list)
                        and {"step", "glb"}.issubset(data["formats"]))
        self.json_check("account_initialized", "api/v1/auth/status", lambda data:
                        data.get("initialized") is True and data.get("bootstrapAllowed") is False)
        for name, path in (("anonymous_identity", "auth/me"), ("anonymous_projects", "pdm/projects")):
            response = self.fetch(name, "api/v1/" + path, expected=401)
            if response is not None:
                self.result(name, True, response[0])
        if self.expected_cad_workspaces:
            # Exact authentication failures establish that the current routers
            # loaded: a missing route (404) or anonymous success must fail.
            for name, path in (
                ("anonymous_native_drawing", "api/cad/drawings"),
                ("anonymous_feature_workspace", "api/cad/features"),
                ("anonymous_engineering_workspace", "api/cad/designs"),
                ("anonymous_delivery_center", "api/cad/deliveries"),
            ):
                response = self.fetch(name, path, expected=401)
                if response is not None:
                    self.result(name, response[0] == 401, response[0],
                                None if response[0] == 401 else "unexpected_http_status")
        if credentials is None:
            return
        session = self.json_check("login", "api/v1/auth/login", lambda data:
                                  isinstance(data.get("access_token"), str) and bool(data["access_token"])
                                  and data.get("token_type", "").lower() == "bearer", payload=credentials)
        if not session:
            return
        token = session["access_token"]
        self.json_check("authenticated_identity", "api/v1/auth/me", lambda data:
                        bool(data.get("id")) and str(data.get("email", "")).casefold() == credentials["email"].casefold(), token=token)
        self.json_check("authenticated_projects", "api/v1/pdm/projects", lambda data:
                        isinstance(data.get("items"), list), token=token)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1", help="Final public origin; redirects are not followed")
    parser.add_argument("--credentials-file", help="JSON file containing email and password; never printed")
    parser.add_argument("--timeout", type=float, default=20, help="Per-request timeout in seconds (default: 20)")
    parser.add_argument("--expected-cad-provider", "--expected-cad-mode", default=os.environ.get("JOYNIU_EXPECTED_CAD_PROVIDER", "").strip() or None,
                        help="Expected CAD engine: relay or codex (env: JOYNIU_EXPECTED_CAD_PROVIDER)")
    parser.add_argument("--expected-cad-model", default=os.environ.get("JOYNIU_EXPECTED_CAD_MODEL", "").strip() or None,
                        help="Exact expected CAD model identifier (env: JOYNIU_EXPECTED_CAD_MODEL)")
    parser.add_argument("--expected-cad-workspaces", action="store_true",
                        help="Require authenticated native drawing, feature, engineering and delivery center routes")
    args = parser.parse_args(argv)
    verifier = Verifier(args.base_url, args.timeout)
    verifier.expected_cad_workspaces = args.expected_cad_workspaces
    credentials = None
    try:
        url = urlsplit(args.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment or not 0 < args.timeout <= 120:
            raise ValueError
        if args.expected_cad_provider is not None:
            expected = args.expected_cad_provider.strip().casefold()
            if expected not in {"relay", "remote", "codex"}:
                verifier.result("configuration", False, error="invalid_expected_cad_provider")
            else:
                verifier.expected_cad_provider = "relay" if expected == "remote" else expected
        if args.expected_cad_model is not None:
            expected = args.expected_cad_model.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", expected):
                verifier.result("configuration", False, error="invalid_expected_cad_model")
            else:
                verifier.expected_cad_model = expected
        if args.credentials_file:
            raw = Path(args.credentials_file).read_bytes()
            if len(raw) > 64 * 1024:
                raise ValueError
            supplied = json.loads(raw)
            if not isinstance(supplied, dict) or not all(isinstance(supplied.get(key), str) and supplied[key].strip() for key in ("email", "password")):
                raise ValueError
            credentials = {"email": supplied["email"].strip(), "password": supplied["password"]}
    except (OSError, ValueError, TypeError):
        verifier.result("configuration", False, error="invalid_url_timeout_or_credentials_file")
    else:
        try:
            if not verifier.checks:
                verifier.run(credentials)
        except Exception:
            # Never include exception text: urllib and parsers can retain
            # request URLs, authorization values or server response content.
            verifier.result("verification", False, error="unexpected_check_failure")
    ok = bool(verifier.checks) and all(check["ok"] for check in verifier.checks)
    report = {"ok": ok, "authenticatedChecksRequested": bool(args.credentials_file), "checks": verifier.checks}
    if verifier.unverified_checks:
        report["unverifiedChecks"] = verifier.unverified_checks
    print(json.dumps(report, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
