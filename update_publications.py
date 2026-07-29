#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# ---------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "publications.json"
REVIEW_PATH = ROOT / "publication-review.json"


# ---------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF_BASE = "https://api.crossref.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"
OPENALEX_BASE = "https://api.openalex.org"


# ---------------------------------------------------------------------
# Shared HTTP session
# ---------------------------------------------------------------------

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": "HolmLabPublicationsBot/2.0"
    }
)


# ---------------------------------------------------------------------
# Basic cleaning helpers
# ---------------------------------------------------------------------

def clean(value: Any) -> str:
    """Convert a value to clean, single-spaced text."""

    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_orcid(value: Any) -> str:
    """Normalize an ORCID into its 0000-0000-0000-0000 form."""

    orcid = clean(value)

    orcid = re.sub(
        r"^https?://orcid\.org/",
        "",
        orcid,
        flags=re.IGNORECASE,
    )

    return orcid.strip().strip("/")


def normalize_doi(value: Any) -> str:
    """Normalize DOI values for matching and URL construction."""

    doi = clean(value).lower()

    prefixes = (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    )

    for prefix in prefixes:
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break

    return doi.strip().strip("/")


def normalize_pmid(value: Any) -> str:
    """Normalize PubMed identifiers."""

    pmid = clean(value)

    pmid = re.sub(
        r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/",
        "",
        pmid,
        flags=re.IGNORECASE,
    )

    return pmid.strip().strip("/")


def normalize_pmc(value: Any) -> str:
    """Normalize PubMed Central identifiers."""

    pmc = clean(value)

    pmc = re.sub(
        r"^https?://www\.ncbi\.nlm\.nih\.gov/pmc/articles/",
        "",
        pmc,
        flags=re.IGNORECASE,
    )

    return pmc.strip().strip("/")


def normalize_title(value: Any) -> str:
    """
    Normalize a title for duplicate matching.

    HTML tags, punctuation, spacing, and capitalization are ignored.
    """

    title = clean(value).lower()
    title = re.sub(r"<[^>]+>", " ", title)
    title = re.sub(r"&[a-z0-9#]+;", " ", title)
    title = re.sub(r"[^a-z0-9]+", "", title)

    return title


def year_from(value: Any) -> int | None:
    """Extract a four-digit publication year."""

    match = re.search(r"\b(19|20)\d{2}\b", clean(value))

    return int(match.group(0)) if match else None


def first_text(
    node: ET.Element | None,
    path: str,
    default: str = "",
) -> str:
    """Get all text under the first matching XML element."""

    if node is None:
        return default

    child = node.find(path)

    if child is None:
        return default

    return clean("".join(child.itertext()))


def first_nonempty(*values: Any) -> str:
    """Return the first nonempty cleaned value."""

    for value in values:
        cleaned = clean(value)

        if cleaned:
            return cleaned

    return ""


def make_pages(first_page: Any, last_page: Any) -> str:
    """Build a page range from first-page and last-page values."""

    first = clean(first_page)
    last = clean(last_page)

    if first and last and first != last:
        return f"{first}-{last}"

    return first or last


# ---------------------------------------------------------------------
# HTTP request helper
# ---------------------------------------------------------------------

def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> Any:
    """
    Make a JSON API request with retry handling.

    Retries rate-limit responses and temporary server failures.
    """

    last_error: Exception | None = None

    for attempt in range(4):
        try:
            response = SESSION.request(
                method,
                url,
                params=params,
                headers=headers,
                data=data,
                timeout=60,
            )

            if response.status_code == 429 or response.status_code >= 500:
                wait_seconds = 2 ** attempt

                print(
                    f"Temporary API response "
                    f"{response.status_code} from {url}; "
                    f"retrying after {wait_seconds} seconds."
                )

                time.sleep(wait_seconds)
                continue

            response.raise_for_status()

            return response.json()

        except requests.RequestException as exc:
            last_error = exc

            if attempt == 3:
                break

            wait_seconds = 2 ** attempt

            print(
                f"Request failed for {url}: {exc}. "
                f"Retrying after {wait_seconds} seconds."
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        f"Repeated request failure for {url}: {last_error}"
    )


# =====================================================================
# PubMed
# =====================================================================

def pubmed_ids(
    query: str,
    email: str,
    api_key: str = "",
) -> list[str]:
    """Retrieve all PubMed IDs matching an author query."""

    params: dict[str, Any] = {
        "db": "pubmed",
        "term": query,
        "retmode": "json",
        "retmax": 10000,
        "tool": "holm_lab_publications",
        "email": email,
    }

    if api_key:
        params["api_key"] = api_key

    data = request_json(
        f"{NCBI_BASE}/esearch.fcgi",
        params=params,
    )

    identifiers = data.get(
        "esearchresult",
        {},
    ).get(
        "idlist",
        [],
    )

    print(
        f'PubMed query "{query}" returned '
        f"{len(identifiers)} records."
    )

    return identifiers


def fetch_pubmed(
    ids: list[str],
    email: str,
    api_key: str = "",
) -> list[dict[str, Any]]:
    """Fetch and normalize PubMed publications."""

    records: list[dict[str, Any]] = []

    for start in range(0, len(ids), 200):
        batch = ids[start:start + 200]

        params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
            "tool": "holm_lab_publications",
            "email": email,
        }

        if api_key:
            params["api_key"] = api_key

        response = SESSION.get(
            f"{NCBI_BASE}/efetch.fcgi",
            params=params,
            timeout=60,
        )

        response.raise_for_status()

        root = ET.fromstring(response.content)

        for article in root.findall(".//PubmedArticle"):
            citation = article.find("MedlineCitation")

            journal_article = (
                citation.find("Article")
                if citation is not None
                else None
            )

            pmid = normalize_pmid(
                first_text(citation, "PMID")
            )

            title = first_text(
                journal_article,
                "ArticleTitle",
            )

            journal = first_text(
                journal_article,
                "Journal/Title",
            )

            abstract_parts = []

            if journal_article is not None:
                abstract_parts = [
                    clean("".join(element.itertext()))
                    for element in journal_article.findall(
                        "Abstract/AbstractText"
                    )
                ]

            authors: list[str] = []

            if journal_article is not None:
                for author in journal_article.findall(
                    "AuthorList/Author"
                ):
                    collective = first_text(
                        author,
                        "CollectiveName",
                    )

                    if collective:
                        authors.append(collective)
                        continue

                    name = " ".join(
                        filter(
                            None,
                            [
                                first_text(author, "ForeName"),
                                first_text(author, "LastName"),
                            ],
                        )
                    )

                    if name:
                        authors.append(name)

            doi = ""
            pmc = ""

            for article_id in article.findall(
                "PubmedData/ArticleIdList/ArticleId"
            ):
                identifier_type = article_id.attrib.get(
                    "IdType",
                    "",
                )

                if identifier_type == "doi":
                    doi = normalize_doi(article_id.text)

                elif identifier_type == "pmc":
                    pmc = normalize_pmc(article_id.text)

            pub_date_node = (
                journal_article.find(
                    "Journal/JournalIssue/PubDate"
                )
                if journal_article is not None
                else None
            )

            date_text = " ".join(
                filter(
                    None,
                    [
                        first_text(pub_date_node, "Year"),
                        first_text(pub_date_node, "Month"),
                        first_text(pub_date_node, "Day"),
                        first_text(pub_date_node, "MedlineDate"),
                    ],
                )
            )

            volume = first_text(
                journal_article,
                "Journal/JournalIssue/Volume",
            )

            issue = first_text(
                journal_article,
                "Journal/JournalIssue/Issue",
            )

            pages = first_text(
                journal_article,
                "Pagination/MedlinePgn",
            )

            if not pages:
                pages = first_text(
                    journal_article,
                    "ELocationID",
                )

            records.append(
                {
                    "title": title,
                    "authors": authors,
                    "journal": journal,
                    "year": year_from(date_text),
                    "date": date_text,
                    "volume": volume,
                    "issue": issue,
                    "pages": pages,
                    "doi": doi,
                    "pmid": pmid,
                    "pmc": pmc,
                    "url": (
                        f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                        if pmid
                        else ""
                    ),
                    "abstract": " ".join(abstract_parts),
                    "type": "journal-article",
                    "sources": ["PubMed"],
                }
            )

    print(f"PubMed supplied {len(records)} normalized records.")

    return records


# =====================================================================
# Crossref
# =====================================================================

def crossref_by_orcid(
    orcid: str,
    email: str,
    include_types: set[str],
) -> list[dict[str, Any]]:
    """Retrieve Crossref records linked to an ORCID."""

    orcid = clean_orcid(orcid)

    if not orcid or "REPLACE_" in orcid:
        return []

    cursor = "*"
    records: list[dict[str, Any]] = []

    while cursor:
        params = {
            "filter": f"orcid:{orcid}",
            "rows": 1000,
            "cursor": cursor,
            "cursor-max": 1000,
            "mailto": email,
        }

        data = request_json(
            f"{CROSSREF_BASE}/works",
            params=params,
        )

        message = data.get("message", {})
        items = message.get("items", [])

        for item in items:
            item_type = clean(item.get("type"))

            if include_types and item_type not in include_types:
                continue

            authors: list[str] = []

            for author in item.get("author", []):
                name = " ".join(
                    filter(
                        None,
                        [
                            clean(author.get("given")),
                            clean(author.get("family")),
                        ],
                    )
                )

                if name:
                    authors.append(name)

            date_parts = (
                item.get(
                    "published-print",
                    {},
                ).get("date-parts")
                or item.get(
                    "published-online",
                    {},
                ).get("date-parts")
                or item.get(
                    "issued",
                    {},
                ).get("date-parts")
                or []
            )

            year = (
                date_parts[0][0]
                if date_parts and date_parts[0]
                else None
            )

            doi = normalize_doi(item.get("DOI"))

            records.append(
                {
                    "title": clean(
                        (item.get("title") or [""])[0]
                    ),
                    "authors": authors,
                    "journal": clean(
                        (item.get("container-title") or [""])[0]
                    ),
                    "year": year,
                    "date": str(year or ""),
                    "volume": clean(item.get("volume")),
                    "issue": clean(item.get("issue")),
                    "pages": clean(
                        item.get("page")
                        or item.get("article-number")
                    ),
                    "doi": doi,
                    "pmid": "",
                    "pmc": "",
                    "url": (
                        f"https://doi.org/{doi}"
                        if doi
                        else clean(item.get("URL"))
                    ),
                    "abstract": clean(item.get("abstract")),
                    "type": item_type,
                    "sources": ["Crossref"],
                }
            )

        next_cursor = message.get("next-cursor")

        if (
            not items
            or not next_cursor
            or next_cursor == cursor
        ):
            break

        cursor = next_cursor

    print(
        f"Crossref supplied {len(records)} records "
        f"for ORCID {orcid}."
    )

    return records


# =====================================================================
# ORCID
# =====================================================================

def get_orcid_token() -> str:
    """
    Obtain an ORCID public API token when credentials are configured.

    If credentials are absent, ORCID is skipped. OpenAlex can still use
    the author's ORCID from config.json.
    """

    client_id = os.getenv(
        "ORCID_CLIENT_ID",
        "",
    ).strip()

    client_secret = os.getenv(
        "ORCID_CLIENT_SECRET",
        "",
    ).strip()

    if not client_id or not client_secret:
        print(
            "ORCID_CLIENT_ID or ORCID_CLIENT_SECRET is missing; "
            "direct ORCID retrieval will be skipped."
        )

        return ""

    data = request_json(
        ORCID_TOKEN_URL,
        method="POST",
        headers={
            "Accept": "application/json",
        },
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "/read-public",
        },
    )

    return clean(data.get("access_token"))


def orcid_works(
    orcid: str,
    token: str,
) -> list[dict[str, Any]]:
    """Retrieve publication summaries from ORCID."""

    orcid = clean_orcid(orcid)

    if (
        not token
        or not orcid
        or "REPLACE_" in orcid
    ):
        return []

    data = request_json(
        f"{ORCID_BASE}/{orcid}/works",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    records: list[dict[str, Any]] = []

    for group in data.get("group", []):
        summaries = group.get("work-summary", [])

        if not summaries:
            continue

        summary = summaries[0]

        external_ids: dict[str, str] = {}

        for external_id in (
            summary.get(
                "external-ids",
                {},
            ).get(
                "external-id",
                [],
            )
        ):
            identifier_type = clean(
                external_id.get("external-id-type")
            ).lower()

            identifier_value = clean(
                external_id.get("external-id-value")
            )

            external_ids[identifier_type] = identifier_value

        doi = normalize_doi(
            external_ids.get("doi")
        )

        pmid = normalize_pmid(
            external_ids.get("pmid")
        )

        title = clean(
            summary.get(
                "title",
                {},
            ).get(
                "title",
                {},
            ).get(
                "value"
            )
        )

        publication_date = (
            summary.get("publication-date")
            or {}
        )

        year = year_from(
            (
                publication_date.get("year")
                or {}
            ).get("value")
        )

        records.append(
            {
                "title": title,
                "authors": [],
                "journal": clean(
                    (
                        summary.get("journal-title")
                        or {}
                    ).get("value")
                ),
                "year": year,
                "date": str(year or ""),
                "volume": "",
                "issue": "",
                "pages": "",
                "doi": doi,
                "pmid": pmid,
                "pmc": "",
                "url": (
                    f"https://doi.org/{doi}"
                    if doi
                    else (
                        f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                        if pmid
                        else ""
                    )
                ),
                "abstract": "",
                "type": clean(
                    summary.get("type")
                ).lower(),
                "sources": ["ORCID"],
            }
        )

    print(
        f"ORCID supplied {len(records)} records "
        f"for ORCID {orcid}."
    )

    return records


# =====================================================================
# OpenAlex
# =====================================================================

def get_openalex_author(
    orcid: str,
    api_key: str,
) -> dict[str, Any] | None:
    """Resolve an ORCID to an OpenAlex author record."""

    orcid = clean_orcid(orcid)

    if not orcid or "REPLACE_" in orcid:
        return None

    if not api_key:
        return None

    try:
        return request_json(
            f"{OPENALEX_BASE}/authors/"
            f"https://orcid.org/{orcid}",
            params={
                "api_key": api_key,
            },
        )

    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            print(
                f"OpenAlex could not find an author for ORCID {orcid}."
            )

            return None

        raise


def openalex_location_source(
    work: dict[str, Any],
) -> dict[str, Any]:
    """Return the best available OpenAlex source object."""

    primary_location = (
        work.get("primary_location")
        or {}
    )

    source = (
        primary_location.get("source")
        or {}
    )

    if source:
        return source

    best_location = (
        work.get("best_oa_location")
        or {}
    )

    source = (
        best_location.get("source")
        or {}
    )

    if source:
        return source

    for location in work.get("locations") or []:
        source = location.get("source") or {}

        if source:
            return source

    return {}


def openalex_full_text_url(
    work: dict[str, Any],
) -> str:
    """Get the best available open-access or PDF URL."""

    best_oa_location = (
        work.get("best_oa_location")
        or {}
    )

    url = first_nonempty(
        best_oa_location.get("pdf_url"),
        best_oa_location.get("landing_page_url"),
    )

    if url:
        return url

    primary_location = (
        work.get("primary_location")
        or {}
    )

    return first_nonempty(
        primary_location.get("pdf_url"),
        primary_location.get("landing_page_url"),
    )


def normalize_openalex_work(
    work: dict[str, Any],
) -> dict[str, Any] | None:
    """Convert one OpenAlex work to the website's publication format."""

    title = clean(
        work.get("title")
        or work.get("display_name")
    )

    if not title:
        return None

    authors: list[str] = []

    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}

        name = clean(
            author.get("display_name")
        )

        if name:
            authors.append(name)

    source = openalex_location_source(work)

    journal = clean(
        source.get("display_name")
    )

    bibliography = (
        work.get("biblio")
        or {}
    )

    volume = clean(
        bibliography.get("volume")
    )

    issue = clean(
        bibliography.get("issue")
    )

    pages = make_pages(
        bibliography.get("first_page"),
        bibliography.get("last_page"),
    )

    doi = normalize_doi(
        work.get("doi")
    )

    identifiers = (
        work.get("ids")
        or {}
    )

    pmid = normalize_pmid(
        identifiers.get("pmid")
    )

    pmc = normalize_pmc(
        identifiers.get("pmcid")
        or identifiers.get("pmc")
    )

    publication_date = clean(
        work.get("publication_date")
    )

    publication_year = (
        work.get("publication_year")
        or year_from(publication_date)
    )

    primary_location = (
        work.get("primary_location")
        or {}
    )

    landing_page_url = clean(
        primary_location.get("landing_page_url")
    )

    openalex_url = clean(
        work.get("id")
    )

    full_text_url = openalex_full_text_url(work)

    url = ""

    if doi:
        url = f"https://doi.org/{doi}"

    elif pmid:
        url = (
            f"https://pubmed.ncbi.nlm.nih.gov/"
            f"{pmid}/"
        )

    elif landing_page_url:
        url = landing_page_url

    elif openalex_url:
        url = openalex_url

    return {
        "title": title,
        "authors": authors,
        "journal": journal,
        "year": publication_year,
        "date": publication_date or str(
            publication_year or ""
        ),
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "doi": doi,
        "pmid": pmid,
        "pmc": pmc,
        "url": url,
        "full_text_url": full_text_url,
        "abstract": "",
        "type": clean(work.get("type")).lower(),
        "openalex_id": openalex_url,
        "sources": ["OpenAlex"],
    }


def openalex_works(
    orcid: str,
    api_key: str,
    include_types: set[str],
) -> list[dict[str, Any]]:
    """
    Retrieve all works assigned to an ORCID-linked OpenAlex author.

    The ORCID is first resolved to the author's OpenAlex ID. The works
    endpoint is then paginated using OpenAlex cursors.
    """

    orcid = clean_orcid(orcid)

    if not orcid or "REPLACE_" in orcid:
        return []

    if not api_key:
        print(
            "OPENALEX_API_KEY is missing; OpenAlex retrieval "
            "will be skipped."
        )

        return []

    author = get_openalex_author(
        orcid,
        api_key,
    )

    if not author:
        return []

    author_id = clean(
        author.get("id")
    ).rstrip("/").split("/")[-1]

    author_name = clean(
        author.get("display_name")
    )

    if not author_id:
        print(
            f"OpenAlex returned no author ID for ORCID {orcid}."
        )

        return []

    print(
        f"OpenAlex author resolved: "
        f"{author_name or 'Unknown author'} "
        f"({author_id})."
    )

    records: list[dict[str, Any]] = []
    cursor = "*"

    while cursor:
        params = {
            "filter": f"authorships.author.id:{author_id}",
            "sort": "-publication_date",
            "per_page": 100,
            "cursor": cursor,
            "api_key": api_key,
        }

        data = request_json(
            f"{OPENALEX_BASE}/works",
            params=params,
        )

        results = data.get("results", [])

        for work in results:
            record = normalize_openalex_work(work)

            if not record:
                continue

            record_type = clean(
                record.get("type")
            )

            /*
             * Apply the configured type filter when OpenAlex's type
             * exactly matches one of the configured values.
             *
             * Common OpenAlex values include:
             * article, book, book-chapter, dataset, dissertation,
             * editorial, letter, preprint, and review.
             *
             * We do not reject "article" merely because config uses
             * Crossref's corresponding name "journal-article".
             */
            type_aliases = {
                "article": "journal-article",
                "book-chapter": "book-chapter",
                "book": "book",
                "dataset": "dataset",
                "dissertation": "dissertation",
                "editorial": "editorial",
                "letter": "letter",
                "preprint": "posted-content",
                "review": "journal-article",
            }

            comparable_type = type_aliases.get(
                record_type,
                record_type,
            )

            if (
                include_types
                and record_type not in include_types
                and comparable_type not in include_types
            ):
                continue

            records.append(record)

        next_cursor = (
            data.get("meta", {})
            .get("next_cursor")
        )

        if (
            not results
            or not next_cursor
            or next_cursor == cursor
        ):
            break

        cursor = next_cursor

    print(
        f"OpenAlex supplied {len(records)} records "
        f"for ORCID {orcid}."
    )

    return records


# =====================================================================
# Record merging and deduplication
# =====================================================================

def title_year_key(
    record: dict[str, Any],
) -> str:
    """Create a title-and-year duplicate key."""

    title = normalize_title(
        record.get("title")
    )

    year = record.get("year") or ""

    if not title:
        return ""

    return f"{title}:{year}"


def record_quality(record: dict[str, Any]) -> int:
    """
    Estimate metadata completeness.

    More complete records are used as the base during duplicate merges.
    """

    score = 0

    weighted_fields = {
        "doi": 6,
        "pmid": 6,
        "title": 5,
        "authors": 5,
        "journal": 4,
        "year": 4,
        "date": 2,
        "volume": 2,
        "issue": 2,
        "pages": 2,
        "abstract": 2,
        "url": 1,
        "full_text_url": 1,
    }

    for field, weight in weighted_fields.items():
        if record.get(field):
            score += weight

    return score


def combine_record_values(
    primary: dict[str, Any],
    secondary: dict[str, Any],
) -> dict[str, Any]:
    """Merge a duplicate record without discarding richer metadata."""

    if record_quality(secondary) > record_quality(primary):
        primary, secondary = secondary, primary

    merged = dict(primary)

    scalar_fields = (
        "title",
        "journal",
        "date",
        "volume",
        "issue",
        "pages",
        "doi",
        "pmid",
        "pmc",
        "url",
        "full_text_url",
        "abstract",
        "type",
        "openalex_id",
    )

    for field in scalar_fields:
        if not merged.get(field) and secondary.get(field):
            merged[field] = secondary[field]

    if not merged.get("year") and secondary.get("year"):
        merged["year"] = secondary["year"]

    primary_authors = merged.get("authors") or []
    secondary_authors = secondary.get("authors") or []

    if (
        not primary_authors
        or len(secondary_authors) > len(primary_authors)
    ):
        merged["authors"] = secondary_authors

    merged["sources"] = sorted(
        set(
            (merged.get("sources") or [])
            + (secondary.get("sources") or [])
        )
    )

    return merged


def records_match(
    first: dict[str, Any],
    second: dict[str, Any],
) -> bool:
    """Determine whether two publication records represent the same work."""

    first_doi = normalize_doi(first.get("doi"))
    second_doi = normalize_doi(second.get("doi"))

    if (
        first_doi
        and second_doi
        and first_doi == second_doi
    ):
        return True

    first_pmid = normalize_pmid(first.get("pmid"))
    second_pmid = normalize_pmid(second.get("pmid"))

    if (
        first_pmid
        and second_pmid
        and first_pmid == second_pmid
    ):
        return True

    first_title = normalize_title(first.get("title"))
    second_title = normalize_title(second.get("title"))

    if (
        first_title
        and second_title
        and first_title == second_title
    ):
        first_year = first.get("year")
        second_year = second.get("year")

        /*
         * Treat title matches as duplicates when:
         * - the years match, or
         * - one source did not provide a year.
         */
        if (
            not first_year
            or not second_year
            or int(first_year) == int(second_year)
        ):
            return True

    return False


def merge_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge duplicate records from PubMed, Crossref, ORCID, and OpenAlex.

    DOI is preferred, followed by PMID, then normalized title and year.
    """

    merged: list[dict[str, Any]] = []

    for record in records:
        matched_index: int | None = None

        for index, current in enumerate(merged):
            if records_match(current, record):
                matched_index = index
                break

        if matched_index is None:
            merged.append(dict(record))
        else:
            merged[matched_index] = combine_record_values(
                merged[matched_index],
                record,
            )

    return merged


# =====================================================================
# Final validation
# =====================================================================

def has_target_author(
    record: dict[str, Any],
    author_config: dict[str, Any],
) -> bool:
    """
    Check whether a record's author list includes the configured author.

    This is only used as a review signal. It does not automatically
    remove the publication because ORCID and Crossref summaries may lack
    complete author information.
    """

    authors = record.get("authors") or []

    if not authors:
        return True

    author_text = clean(
        " ".join(str(author) for author in authors)
    ).lower()

    configured_names = [
        clean(author_config.get("name")),
        clean(author_config.get("display_name")),
        "Johanna Holm",
        "Johanna B Holm",
        "Holm JB",
    ]

    for name in configured_names:
        normalized_name = clean(name).lower()

        if normalized_name and normalized_name in author_text:
            return True

    /*
     * This function ends below. This comment is intentionally replaced
     * by valid Python logic in the next lines.
     */

    return (
        "johanna holm" in author_text
        or "johanna b holm" in author_text
        or "holm jb" in author_text
    )


def add_unique_review_record(
    review: list[dict[str, Any]],
    reason: str,
    record: dict[str, Any],
) -> None:
    """Add one review item without creating identical duplicates."""

    review_key = (
        reason,
        normalize_doi(record.get("doi")),
        normalize_pmid(record.get("pmid")),
        normalize_title(record.get("title")),
        record.get("year"),
    )

    for existing in review:
        existing_record = existing.get("record", {})

        existing_key = (
            existing.get("reason"),
            normalize_doi(existing_record.get("doi")),
            normalize_pmid(existing_record.get("pmid")),
            normalize_title(existing_record.get("title")),
            existing_record.get("year"),
        )

        if existing_key == review_key:
            return

    review.append(
        {
            "reason": reason,
            "record": record,
        }
    )


# =====================================================================
# Main pipeline
# =====================================================================

def main() -> int:
    """Run the publication update pipeline."""

    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Configuration file was not found: {CONFIG_PATH}"
        )

    config = json.loads(
        CONFIG_PATH.read_text(
            encoding="utf-8"
        )
    )

    email = clean(
        config.get("contact_email")
    )

    if not email or "YOUR_EMAIL" in email:
        raise RuntimeError(
            "Replace contact_email in config.json "
            "with a real email address."
        )

    ncbi_key = os.getenv(
        "NCBI_API_KEY",
        "",
    ).strip()

    openalex_api_key = os.getenv(
        "OPENALEX_API_KEY",
        "",
    ).strip()

    include_types = {
        clean(item).lower()
        for item in config.get(
            "include_types",
            [],
        )
        if clean(item)
    }

    all_records: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    orcid_token = get_orcid_token()

    authors_config = config.get(
        "authors",
        [],
    )

    if not authors_config:
        raise RuntimeError(
            "No authors were configured in config.json."
        )

    for author in authors_config:
        query = clean(
            author.get("pubmed_query")
        )

        orcid = clean_orcid(
            author.get("orcid")
        )

        author_name = first_nonempty(
            author.get("name"),
            author.get("display_name"),
            orcid,
            query,
        )

        print()
        print(
            f"Retrieving publications for "
            f"{author_name or 'configured author'}..."
        )

        if query:
            identifiers = pubmed_ids(
                query,
                email,
                ncbi_key,
            )

            pubmed_records = fetch_pubmed(
                identifiers,
                email,
                ncbi_key,
            )

            all_records.extend(pubmed_records)

        if orcid and "REPLACE_" not in orcid:
            crossref_records = crossref_by_orcid(
                orcid,
                email,
                include_types,
            )

            direct_orcid_records = orcid_works(
                orcid,
                orcid_token,
            )

            openalex_records = openalex_works(
                orcid,
                openalex_api_key,
                include_types,
            )

            all_records.extend(crossref_records)
            all_records.extend(direct_orcid_records)
            all_records.extend(openalex_records)

            for record in openalex_records:
                if not has_target_author(record, author):
                    add_unique_review_record(
                        review,
                        "OpenAlex author list does not clearly include "
                        "the configured author",
                        record,
                    )

        elif not query:
            print(
                "The author has neither a PubMed query nor a valid "
                "ORCID, so no source can be searched."
            )

    print()
    print(
        f"Retrieved {len(all_records)} total source records "
        f"before deduplication."
    )

    publications = merge_records(
        all_records
    )

    print(
        f"{len(publications)} unique publication records remain "
        f"after deduplication."
    )

    earliest = int(
        config.get(
            "earliest_year",
            1900,
        )
    )

    excluded_dois = {
        normalize_doi(value)
        for value in config.get(
            "exclude_dois",
            [],
        )
        if normalize_doi(value)
    }

    excluded_pmids = {
        normalize_pmid(value)
        for value in config.get(
            "exclude_pmids",
            [],
        )
        if normalize_pmid(value)
    }

    excluded_titles = {
        normalize_title(value)
        for value in config.get(
            "exclude_titles",
            [],
        )
        if normalize_title(value)
    }

    final: list[dict[str, Any]] = []

    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

    for publication in publications:
        publication_year = publication.get("year")

        if (
            publication_year
            and int(publication_year) < earliest
        ):
            continue

        doi = normalize_doi(
            publication.get("doi")
        )

        pmid = normalize_pmid(
            publication.get("pmid")
        )

        title_key = normalize_title(
            publication.get("title")
        )

        if doi and doi in excluded_dois:
            continue

        if pmid and pmid in excluded_pmids:
            continue

        if title_key and title_key in excluded_titles:
            continue

        if not publication.get("title"):
            add_unique_review_record(
                review,
                "Missing title",
                publication,
            )

            continue

        if not publication.get("year"):
            add_unique_review_record(
                review,
                "Missing publication year",
                publication,
            )

        publication["doi"] = doi
        publication["pmid"] = pmid
        publication["pmc"] = normalize_pmc(
            publication.get("pmc")
        )

        identifier_text = (
            doi
            or pmid
            or (
                f"{title_key}:"
                f"{publication.get('year') or ''}"
            )
        )

        publication["id"] = hashlib.sha1(
            identifier_text.encode("utf-8")
        ).hexdigest()[:12]

        publication["updated_at"] = timestamp

        publication["sources"] = sorted(
            set(
                publication.get("sources")
                or []
            )
        )

        final.append(publication)

    final.sort(
        key=lambda publication: (
            -(publication.get("year") or 0),
            clean(
                publication.get("title")
            ).lower(),
        )
    )

    OUTPUT_PATH.write_text(
        json.dumps(
            final,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    REVIEW_PATH.write_text(
        json.dumps(
            review,
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(
        f"Wrote {len(final)} publications to "
        f"{OUTPUT_PATH.name}."
    )

    print(
        f"Wrote {len(review)} review records to "
        f"{REVIEW_PATH.name}."
    )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(
            main()
        )

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )

        raise
