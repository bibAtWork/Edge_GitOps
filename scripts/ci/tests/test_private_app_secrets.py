"""Guard the private-package preflight before any SOPS file is written."""

import importlib.util
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "private_app_secrets", ROOT / "bootstrap/scripts/provision-private-app-secrets.py")
private = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(private)


class Response(BytesIO):
    status = 200


class PrivateAppSecretTests(unittest.TestCase):
    def test_gemini_document_extracts_only_api_key(self):
        path = Path("gemini-key.txt")
        with patch.object(Path, "resolve", return_value=Path("C:/outside/gemini-key.txt")), \
             patch.object(Path, "read_text", return_value="api-key: example-key\nproject: 123456\n"):
            self.assertEqual(private.read_gemini_key(path), "example-key")

    def test_gemini_document_rejects_other_fields(self):
        path = Path("gemini-key.txt")
        with patch.object(Path, "resolve", return_value=Path("C:/outside/gemini-key.txt")), \
             patch.object(Path, "read_text", return_value="api-key: example-key\nother: value\n"):
            with self.assertRaisesRegex(ValueError, "expected api-key"):
                private.read_gemini_key(path)

    def test_every_package_must_be_readable(self):
        responses = []
        for _ in private.PACKAGES:
            responses.extend((Response(b'{"token":"exchange"}'), Response(b'{}')))
        with patch.object(private.urllib.request, "urlopen", side_effect=responses) as open_url:
            private.verify_packages("owner", "example-token")
        self.assertEqual(open_url.call_count, len(private.PACKAGES) * 2)
        urls = [call.args[0].full_url for call in open_url.call_args_list]
        for package in private.PACKAGES:
            self.assertTrue(any(f"/{package}/tags/list" in url for url in urls))

    def test_denied_package_fails_before_secret_generation(self):
        denied = HTTPError("https://ghcr.io/v2/bibatwork/sre-agent-api/tags/list",
                           403, "forbidden", {}, None)
        with patch.object(private.urllib.request, "urlopen",
                          side_effect=[Response(b'{"token":"exchange"}'), denied]):
            with self.assertRaisesRegex(ValueError, "sre-agent-api.*HTTP 403"):
                private.verify_packages("owner", "example-token")
        denied.close()


if __name__ == "__main__":
    unittest.main()
