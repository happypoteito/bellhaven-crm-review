import ssl
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

from scrape_bellhaven import BASE_URL, fetch_html, find_communities, read_community


class BellhavenScraperTest(TestCase):
    def test_fetch_uses_macos_ca_bundle_when_python_has_no_default_paths(self):
        paths = ssl.get_default_verify_paths()._replace(cafile=None, capath=None)
        context = Mock()
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"<h1>Bellhaven</h1>"

        def open_verified(url, timeout, context=None):
            if context is None or not context.load_verify_locations.called:
                raise URLError(ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED"))
            return response

        with patch.dict("os.environ", clear=True), patch("sys.platform", "darwin"):
            with patch("ssl.get_default_verify_paths", return_value=paths), patch("pathlib.Path.is_file", return_value=True):
                with patch("ssl.create_default_context", return_value=context), patch("scrape_bellhaven.urlopen", side_effect=open_verified):
                    self.assertEqual(fetch_html(BASE_URL + "/"), "<h1>Bellhaven</h1>")
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_combines_paginated_directory_with_homepage_and_reads_detail_fields(self):
        pages = {
            "/": (
                '<a href="/communities">Directory</a><a href="/communities/new">New</a>'
                '<a href="/communities/first#about">First</a>'
            ),
            "/communities": (
                '<p>Page 1 of 2 · 2 communities listed</p><a href="/communities/first">First</a>'
                '<a href="?page=2">Next</a>'
            ),
            "/communities?page=2": (
                '<p>Page 2 of 2 · 2 communities listed</p>'
                '<a href="/communities/other-manor">Other Manor</a>'
                '<a href="?page=1">Previous</a>'
            ),
            "/communities/new": (
                '<h1>The Gardens at BELLHAVEN &amp; Co.</h1><dl>'
                '<dt>Address</dt><dd>10 Main St<br/>Suite 200<br>North Haven, CT 06473</dd>'
                '<dt>Care Offerings</dt><dd><span>Assisted Living</span>\n'
                '<span>Memory Support</span></dd></dl>'
            ),
            "/communities/other-manor": '<h1>Other Manor</h1>',
            "/communities/bellhavenish": '<h1>Bellhavenish Manor</h1>',
        }
        with patch("scrape_bellhaven.fetch_html", side_effect=lambda url: pages[url.removeprefix(BASE_URL)]):
            self.assertEqual(
                find_communities(),
                [BASE_URL + "/communities/" + slug for slug in ("first", "new", "other-manor")],
            )
            self.assertEqual(
                read_community(BASE_URL + "/communities/new"),
                (
                    "The Gardens at BELLHAVEN & Co.", "10 Main St, Suite 200", "North Haven", "CT", "06473",
                    "Assisted Living | Memory Support",
                ),
            )
            self.assertIsNone(read_community(BASE_URL + "/communities/other-manor"))
            self.assertIsNone(read_community(BASE_URL + "/communities/bellhavenish"))
            pages["/communities"] = pages["/communities"].split('<a href="?page=2">')[0]
            with self.assertRaisesRegex(ValueError, "Directory coverage check failed"):
                find_communities()
