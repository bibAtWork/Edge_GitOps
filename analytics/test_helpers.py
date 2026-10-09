import unittest
import hashlib
from export_policy import policy_from_manifest
from prepare import password_hash


class HelperTests(unittest.TestCase):
    def test_hash_is_salted_and_valid(self):
        iterations, salt, digest = password_hash("synthetic").split(":")
        self.assertEqual(hashlib.pbkdf2_hmac("sha256", b"synthetic", bytes.fromhex(salt), int(iterations)).hex(), digest)
        self.assertNotEqual(password_hash("synthetic"), password_hash("synthetic"))

    def test_policy_extraction_fails_on_missing_key(self):
        with self.assertRaises(KeyError):
            policy_from_manifest("data: {}")

    def test_policy_extracts_literal(self):
        self.assertEqual(policy_from_manifest('data:\n  analytics-trino.rego: "package example\\n"'), 'package example\n')


if __name__ == "__main__":
    unittest.main()
