"""Check publication guards using fabricated values, without network or secrets."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('publication_guard', Path(__file__).resolve().parents[1] / 'scripts/check_publication.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class PublicationTests(unittest.TestCase):
    def test_private_paths_remain_blocked_even_if_force_added(self):
        for path in ('runtime/trace.json', 'nested/config.local.json', '.env.private', 'nested/config.toml', 'key.pem'):
            self.assertTrue(guard.forbidden_path(path), path)
        self.assertFalse(guard.forbidden_path('test/fixtures/synthetic_research_package.md'))

    def test_tokens_and_private_literals_are_reported_without_values(self):
        value = 'sk-' + '1234567890abcdef' * 2
        self.assertEqual(guard.inspect_bytes(value.encode()), ['credential_pattern'])
        self.assertEqual(guard.inspect_bytes(b'private-marker', ['private-marker']), ['private_value'])

    def test_unreviewed_routing_is_blocked_but_public_sources_are_allowed(self):
        address = 'https://' + 'unreviewed-host' + '.com/v1'
        self.assertIn('unreviewed_url_host', guard.inspect_bytes(address.encode()))
        self.assertFalse(guard.inspect_bytes(b'https://static.cninfo.com.cn/announcement.pdf'))
        self.assertFalse(guard.inspect_bytes(b'https://example.invalid/v1'))

    def test_url_credentials_are_rejected_even_on_public_host(self):
        address = 'https://' + 'sample:password@' + 'github.com/owner/repo'
        self.assertIn('url_credentials', guard.inspect_bytes(address.encode()))

    def test_unconfigured_checkout_does_not_read_home_credentials_or_send_requests(self):
        from finresearch.storage import configuration
        from finresearch.provider import APIProvider, ProviderError
        config = configuration()
        self.assertEqual(config['api']['credential_source'], 'environment')
        with patch.dict('os.environ', {}, clear=True), \
                patch('finresearch.provider.Path.read_text', side_effect=AssertionError('No credential file access')), \
                patch('finresearch.provider.urllib.request.build_opener') as opener:
            provider = APIProvider(config)
            self.assertFalse(provider.ready())
            with self.assertRaises(ProviderError):
                provider.call('business', 'instructions', 'document', {})
            opener.assert_not_called()
