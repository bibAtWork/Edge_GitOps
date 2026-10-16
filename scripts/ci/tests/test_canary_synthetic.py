"""The synthetic route check must remain truthful during an OTel outage."""

from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import yaml


ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "cluster/base/applications/canary/synthetic.yaml"
ISSUER = "https://keycloak.homelab.data-harness.org/realms/homelab/"


def run_check(route_status: int, export_fails: bool) -> None:
    source = next(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))["data"]["check.py"]
    route_error = HTTPError(
        "https://canary.homelab.data-harness.org/",
        route_status,
        "response",
        {"Location": ISSUER + "protocol/openid-connect/auth" if route_status == 302 else ""},
        None,
    )
    export_error = HTTPError("http://otel/v1/metrics", 503, "unavailable", {}, None)

    with patch("urllib.request.build_opener") as opener, patch("urllib.request.urlopen") as export:
        opener.return_value.open.side_effect = route_error
        if export_fails:
            export.side_effect = export_error
        else:
            export.return_value.__enter__.return_value.status = 200
        exec(compile(source, str(MANIFEST), "exec"), {"__name__": "__main__"})


class SyntheticCheckTests(unittest.TestCase):
    def test_successful_route_check_survives_export_failure(self):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            run_check(302, export_fails=True)
        self.assertIn('"oidc_redirect": "verified"', stdout.getvalue())
        self.assertIn("synthetic metric export failed", stderr.getvalue())

    def test_route_failure_is_not_masked_by_export_failure(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaisesRegex(RuntimeError, "unexpected OIDC response: status=401"):
                run_check(401, export_fails=True)
        self.assertIn("synthetic metric export failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
