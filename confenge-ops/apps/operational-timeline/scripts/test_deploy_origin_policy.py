#!/usr/bin/env python3
import unittest

from deploy_origin_policy import (
    CANONICAL_LOOPBACK_ORIGIN,
    CANONICAL_PUBLIC_ORIGIN,
    validate_api_origin,
    validate_public_origin,
)


class DeployOriginPolicyTest(unittest.TestCase):
    def test_accepts_only_canonical_public_origin(self):
        self.assertEqual(
            validate_public_origin(CANONICAL_PUBLIC_ORIGIN),
            CANONICAL_PUBLIC_ORIGIN,
        )
        for value in (
            "https://attacker.invalid",
            "https://gestao.confenge.com.br.attacker.invalid",
            "https://user@gestao.confenge.com.br",
            "https://gestao.confenge.com.br/path",
            "https://gestao.confenge.com.br?next=attacker",
            "https://gestao.confenge.com.br:443",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_origin(value)

    def test_api_origin_is_canonical_or_exact_loopback(self):
        self.assertEqual(
            validate_api_origin(CANONICAL_PUBLIC_ORIGIN),
            CANONICAL_PUBLIC_ORIGIN,
        )
        self.assertEqual(
            validate_api_origin(CANONICAL_LOOPBACK_ORIGIN),
            CANONICAL_LOOPBACK_ORIGIN,
        )
        for value in (
            "http://localhost:18300",
            "http://127.0.0.1:18301",
            "http://127.0.0.1:18300/path",
            "https://attacker.invalid",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_api_origin(value)


if __name__ == "__main__":
    unittest.main()
