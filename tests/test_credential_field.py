import unittest
from dataclasses import FrozenInstanceError

from core.exchange_accounts.base import CredentialField


class CredentialFieldTests(unittest.TestCase):
    def test_passphrase_factory_creates_required_password(self):
        field = CredentialField.passphrase()

        self.assertEqual(field.name, "passphrase")
        self.assertEqual(field.field_type, "password")
        self.assertTrue(field.required)
        with self.assertRaises(FrozenInstanceError):
            field.name = "changed"

    def test_passphrase_serializes_for_the_account_form(self):
        field = CredentialField.passphrase()

        self.assertEqual(field.to_dict(), {
            "name": "passphrase",
            "label": "API Passphrase",
            "required": True,
            "type": "password",
        })


if __name__ == "__main__":
    unittest.main()
