#!/usr/bin/env python3
"""Export the community locations published on Bellhaven's public website."""

import argparse
from collections import deque
import csv
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import ssl
import sys
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen


BASE_URL = "https://analyst-assessment-production.up.railway.app"
CSV_FIELDS = ("name", "address", "city", "state", "zip", "care_offerings")


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.name = ""
        self.details = {}
        self._tag = None
        self._label = ""
        self._parts = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        elif tag in ("h1", "dt", "dd"):
            self._tag = tag
            self._parts = [[]]
        elif tag == "br" and self._tag == "dd":
            self._parts.append([])

    def handle_data(self, data):
        if self._tag:
            self._parts[-1].append(data)

    def handle_endtag(self, tag):
        if tag == "span" and self._tag == "dd" and self._label == "Care Offerings":
            self._parts.append([])
        if tag != self._tag:
            return

        parts = [" ".join("".join(part).split()) for part in self._parts]
        if tag == "h1":
            self.name = parts[0]
        elif tag == "dt":
            self._label = parts[0]
        elif tag == "dd":
            self.details[self._label] = [part for part in parts if part]
        self._tag = None


def https_context():
    context = ssl.create_default_context()
    if sys.platform == "darwin":
        paths = ssl.get_default_verify_paths()
        explicit_ca = paths.openssl_cafile_env in os.environ or paths.openssl_capath_env in os.environ
        macos_ca_bundle = Path("/etc/ssl/cert.pem")
        if not (paths.cafile or paths.capath or explicit_ca) and macos_ca_bundle.is_file():
            context.load_verify_locations(cafile=str(macos_ca_bundle))
    return context


def fetch_html(url):
    try:
        with urlopen(url, timeout=20, context=https_context()) as response:
            return response.read().decode(response.headers.get_content_charset() or "utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Could not read {url}: {error}") from error


def find_communities():
    home = BASE_URL + "/"
    origin = urlsplit(home)[:2]
    pending = deque([home])
    seen = {home}
    communities = set()
    directory_communities = set()
    directory_counts = set()

    while pending:
        url = pending.popleft()
        html = fetch_html(url)
        is_directory = urlsplit(url).path == "/communities"
        if is_directory:
            count = re.search(r"\b(\d+)\s+communities listed\b", html)
            if not count:
                raise ValueError(f"Could not read the directory's advertised count at {url}")
            directory_counts.add(int(count[1]))

        for href in Page(html).links:
            link = urlsplit(urljoin(url, href))
            if link[:2] != origin:
                continue
            path = link.path.rstrip("/") or "/"
            if re.fullmatch(r"/communities/[^/]+", path):
                community = BASE_URL + path
                communities.add(community)
                if is_directory:
                    directory_communities.add(community)
            elif path in ("/", "/about", "/communities"):
                page = BASE_URL + path
                if path == "/communities" and link.query not in ("", "page=1"):
                    if not re.fullmatch(r"page=[1-9][0-9]*", link.query):
                        continue
                    page += "?" + link.query
                if page not in seen:
                    pending.append(page)
                    seen.add(page)

    if not communities or directory_counts != {len(directory_communities)}:
        raise ValueError(
            f"Directory coverage check failed: found {len(directory_communities)} listings, "
            f"with advertised counts {sorted(directory_counts)}"
        )
    return sorted(communities)


def read_community(url):
    page = Page(fetch_html(url))
    if not page.name:
        raise ValueError(f"Missing community name at {url}")
    if not re.search(r"\bbellhaven\b", page.name, re.IGNORECASE):
        return None

    address = page.details.get("Address", [])
    offerings = page.details.get("Care Offerings", [])
    if len(address) < 2 or not offerings:
        raise ValueError(f"Missing address or care offerings at {url}")

    locality = re.fullmatch(r"(.+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", address[-1])
    if not locality:
        raise ValueError(f"Could not split the city, state, and ZIP at {url}: {address[-1]}")
    city, state, zip_code = locality.groups()
    return page.name, ", ".join(address[:-1]), city.strip(), state, zip_code, " | ".join(offerings)


def collect_locations():
    locations = map(read_community, find_communities())
    rows = sorted((row for row in locations if row is not None), key=lambda row: row[0].casefold())
    if not rows:
        raise ValueError("No community names contained the word Bellhaven")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=Path("bellhaven_locations.csv"))
    args = parser.parse_args()

    try:
        rows = collect_locations()
        with TemporaryDirectory(dir=args.output.parent) as temp_dir:
            temporary_csv = Path(temp_dir) / args.output.name
            with temporary_csv.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(CSV_FIELDS)
                writer.writerows(rows)
            temporary_csv.replace(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")

    print(f"Wrote {len(rows)} community locations to {args.output}")


if __name__ == "__main__":
    main()
