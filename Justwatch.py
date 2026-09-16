#!/usr/bin/env python3
"""
justwatch_scraper.py

Scrapes JustWatch (https://www.justwatch.com/) for movie/TV title data:
which streaming services carry a title, in which country, and under what
offer type (subscription, rent, buy, free, ads).

JustWatch's website is a heavily JS-rendered React app, so plain HTML
scraping (requests + BeautifulSoup) won't see any content. Instead this
script talks to the same public GraphQL API that justwatch.com itself
calls from the browser. No API key is required.

NOTE: This is an unofficial API. JustWatch can change it without notice,
and heavy/automated use may be against their Terms of Service — this
script is intended for light, personal, non-commercial use (e.g. checking
where a handful of titles are streaming). Respect robots.txt and rate
limits, and consider JustWatch's own affiliate/partner API
(https://www.justwatch.com/us/api-content) for anything commercial.

Usage:
    python justwatch_scraper.py "The Matrix" --country US
    python justwatch_scraper.py "Breaking Bad" --country GB --content-type show
    python justwatch_scraper.py "Dune" --country US --json

Requires: requests  (pip install requests)
"""

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

import requests

GRAPHQL_URL = "https://apis.justwatch.com/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Origin": "https://www.justwatch.com",
    "Referer": "https://www.justwatch.com/",
}

# Minimal GraphQL query mirroring what the JustWatch frontend sends when
# you type into the search box. Trimmed down to the fields we care about.
SEARCH_QUERY = """
query GetSearchTitles($searchTitlesFilter: TitleFilter!, $country: Country!, $language: Language!, $first: Int!) {
  popularTitles: popularTitles(
    country: $country
    filter: $searchTitlesFilter
    first: $first
  ) {
    edges {
      node {
        id
        objectType
        objectId
        content(country: $country, language: $language) {
          title
          originalReleaseYear
          fullPath
        }
        offers(country: $country, platform: WEB) {
          monetizationType
          presentationType
          package {
            clearName
            technicalName
          }
          url
          currency
          retailPrice
        }
      }
    }
  }
}
"""


def search_titles(
    query: str,
    country: str = "US",
    language: str = "en",
    content_type: Optional[str] = None,
    limit: int = 10,
    session: Optional[requests.Session] = None,
) -> List[Dict[str, Any]]:
    """
    Query JustWatch's GraphQL API for titles matching `query`.

    content_type: None for all, or "movie" / "show".
    Returns a list of raw title nodes (dicts) from the API.
    """
    sess = session or requests.Session()

    title_filter: Dict[str, Any] = {"searchQuery": query}
    if content_type == "movie":
        title_filter["objectTypes"] = ["MOVIE"]
    elif content_type == "show":
        title_filter["objectTypes"] = ["SHOW"]

    payload = {
        "operationName": "GetSearchTitles",
        "query": SEARCH_QUERY,
        "variables": {
            "searchTitlesFilter": title_filter,
            "country": country.upper(),
            "language": language,
            "first": limit,
        },
    }

    resp = sess.post(GRAPHQL_URL, headers=HEADERS, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "errors" in data:
        raise RuntimeError(f"JustWatch API returned errors: {data['errors']}")

    edges = data.get("data", {}).get("popularTitles", {}).get("edges", [])
    return [edge["node"] for edge in edges]


def summarize_offers(offers: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Group raw offer dicts into a simple {offer_type: [provider names]} map."""
    grouped: Dict[str, List[str]] = {}
    for offer in offers or []:
        mtype = offer.get("monetizationType", "UNKNOWN")
        provider = (offer.get("package") or {}).get("clearName", "Unknown")
        grouped.setdefault(mtype, [])
        if provider not in grouped[mtype]:
            grouped[mtype].append(provider)
    return grouped


def format_title(node: Dict[str, Any], country: str) -> Dict[str, Any]:
    content = node.get("content", {}) or {}
    return {
        "title": content.get("title"),
        "year": content.get("originalReleaseYear"),
        "type": node.get("objectType"),
        "justwatch_url": (
            f"https://www.justwatch.com{content.get('fullPath')}"
            if content.get("fullPath")
            else None
        ),
        "country": country.upper(),
        "offers": summarize_offers(node.get("offers", [])),
        # Raw offers kept separately so we can pull each provider's deep
        # link (offer["url"]) for the M3U export below. These are links
        # to the title's page on the legal provider (e.g. a Netflix or
        # Prime Video URL) — not direct video stream files.
        "raw_offers": node.get("offers", []),
    }


def build_m3u(results: List[Dict[str, Any]]) -> str:
    """
    Build an M3U playlist where each entry is a legal provider deep link
    for a title (from JustWatch's offers), not a direct video stream.
    Handy as a personal "where to watch" bookmark list opened in a player
    or browser that supports M3U.
    """
    lines = ["#EXTM3U"]
    for result in results:
        title = result.get("title") or "Unknown title"
        year = result.get("year")
        label_base = f"{title} ({year})" if year else title

        seen_urls = set()
        for offer in result.get("raw_offers", []):
            url = offer.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            provider = (offer.get("package") or {}).get("clearName", "Unknown")
            mtype = offer.get("monetizationType", "")
            label = {
                "FLATRATE": "Subscription",
                "RENT": "Rent",
                "BUY": "Buy",
                "FREE": "Free",
                "ADS": "Free (ads)",
            }.get(mtype, mtype.title())

            entry_name = f"{label_base} - {provider} ({label})"
            lines.append(f"#EXTINF:-1,{entry_name}")
            lines.append(url)

        if not seen_urls and result.get("justwatch_url"):
            # No direct provider links found; fall back to the JustWatch
            # title page itself so the entry isn't lost.
            lines.append(f"#EXTINF:-1,{label_base} - JustWatch page")
            lines.append(result["justwatch_url"])

    return "\n".join(lines) + "\n"


def print_result(result: Dict[str, Any]) -> None:
    print(f"\n{result['title']} ({result['year']}) [{result['type']}]")
    print(f"  JustWatch: {result['justwatch_url']}")
    if not result["offers"]:
        print(f"  No streaming offers found in {result['country']}.")
        return
    for offer_type, providers in result["offers"].items():
        label = {
            "FLATRATE": "Subscription",
            "RENT": "Rent",
            "BUY": "Buy",
            "FREE": "Free",
            "ADS": "Free (with ads)",
        }.get(offer_type, offer_type.title())
        print(f"  {label}: {', '.join(providers)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Search JustWatch for where a title is streaming."
    )
    parser.add_argument("query", help="Movie or TV show title to search for")
    parser.add_argument(
        "--country",
        default="US",
        help="Two-letter country code, e.g. US, GB, DE (default: US)",
    )
    parser.add_argument(
        "--content-type",
        choices=["movie", "show"],
        default=None,
        help="Restrict results to movies or TV shows",
    )
    parser.add_argument(
        "--limit", type=int, default=5, help="Max number of results (default: 5)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Print raw JSON instead of formatted text"
    )
    parser.add_argument(
        "--m3u",
        metavar="FILE",
        help="Write results as an M3U playlist of legal provider links to FILE",
    )
    args = parser.parse_args()

    try:
        nodes = search_titles(
            query=args.query,
            country=args.country,
            content_type=args.content_type,
            limit=args.limit,
        )
    except requests.HTTPError as e:
        print(f"HTTP error contacting JustWatch: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not nodes:
        print(f"No results for '{args.query}'.")
        return

    results = [format_title(node, args.country) for node in nodes]

    if args.m3u:
        playlist = build_m3u(results)
        with open(args.m3u, "w", encoding="utf-8") as f:
            f.write(playlist)
        print(f"Wrote M3U playlist with legal provider links to {args.m3u}")
        return

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print_result(result)
            time.sleep(0)  # placeholder if you add rate limiting for batches


if __name__ == "__main__":
    main()
