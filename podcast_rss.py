#!/usr/bin/env python3
"""Look up a podcast's RSS feed URL using the Podcast Index API.

Requires free API credentials from https://api.podcastindex.org/
Set them via environment variables:

    export PODCASTINDEX_API_KEY="your-key"
    export PODCASTINDEX_API_SECRET="your-secret"

Usage:
    python3 podcast_rss.py "the daily"
    python3 podcast_rss.py --all "history podcast"   # list all matches
    python3 podcast_rss.py --json "99% invisible"    # raw JSON output
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API_BASE = 'https://api.podcastindex.org/api/1.0'


def build_headers(api_key, api_secret):
    # Podcast Index auth: sha1(key + secret + unix_time) with matching X-Auth-Date
    auth_date = str(int(time.time()))
    auth_hash = hashlib.sha1((api_key + api_secret + auth_date).encode()).hexdigest()
    return {
        'User-Agent': 'podcast-rss-lookup/1.0',
        'X-Auth-Key': api_key,
        'X-Auth-Date': auth_date,
        'Authorization': auth_hash,
    }


def search_podcasts(query, api_key, api_secret, max_results=10):
    params = urllib.parse.urlencode({'q': query, 'max': max_results})
    url = f'{API_BASE}/search/byterm?{params}'
    request = urllib.request.Request(url, headers=build_headers(api_key, api_secret))
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description='Get the RSS feed URL of a podcast via Podcast Index')
    parser.add_argument('query', help='podcast name or search terms')
    parser.add_argument('--all', action='store_true', help='show all matching feeds instead of just the best match')
    parser.add_argument('--json', action='store_true', help='print the raw JSON API response')
    args = parser.parse_args()

    api_key = os.environ.get('PODCASTINDEX_API_KEY')
    api_secret = os.environ.get('PODCASTINDEX_API_SECRET')
    if not api_key or not api_secret:
        sys.exit(
            'Error: PODCASTINDEX_API_KEY and PODCASTINDEX_API_SECRET must be set.\n'
            'Get free credentials at https://api.podcastindex.org/'
        )

    try:
        data = search_podcasts(args.query, api_key, api_secret)
    except urllib.error.HTTPError as error:
        sys.exit(f'API request failed: HTTP {error.code} {error.reason}')
    except urllib.error.URLError as error:
        sys.exit(f'Network error: {error.reason}')

    if args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        return

    feeds = data.get('feeds') or []
    if not feeds:
        sys.exit(f'No podcasts found for: {args.query}')

    if args.all:
        for feed in feeds:
            print(f"{feed.get('title', '(untitled)')}\n  by:  {feed.get('author', 'unknown')}\n  rss: {feed.get('url')}\n")
    else:
        best = feeds[0]
        print(f"Title:  {best.get('title', '(untitled)')}")
        print(f"Author: {best.get('author', 'unknown')}")
        print(f"RSS:    {best.get('url')}")
        if len(feeds) > 1:
            print(f'\n({len(feeds) - 1} more matches; rerun with --all to see them)')


if __name__ == '__main__':
    main()
