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
from typing import Any, Callable

import requests


# =====================================================================
# File paths
# =====================================================================

SCRIPT_DIR = Path(__file__).resolve().parent

if (SCRIPT_DIR / "config.json").exists():
    ROOT = SCRIPT_DIR
else:
    ROOT = SCRIPT_DIR.parent

CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "publications.json"
REVIEW_PATH = ROOT / "publication-review.json"


# =====================================================================
# API endpoints
# =====================================================================

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF_BASE = "https://api.crossref.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"
OPENALEX_BASE = "https://api.openalex.org"


# =====================================================================
# Default institutional affiliation filter
# =====================================================================

DEFAULT_ALLOWED_AFFILIATIONS = [
    "Millersville University",
    "Cornell University",
    "University of Southern California",
    "University of Maryland School of Medicine",
]


AFFILIATION_ALIASES = {
    "millersville university": [
        "millersville university",
        "millersville university of pennsylvania",
    ],
    "cornell university": [
        "cornell university",
        "weill cornell",
        "weill cornell medicine",
        "weill medical college of cornell university",
        "weill cornell medical college",
    ],
    "university of southern california": [
        "university of southern california",
        "keck school of medicine",
        "keck school of medicine of usc",
        "usc keck school of medicine",
    ],
    "university of maryland school of medicine": [
        "university of maryland school of medicine",
        "university of maryland baltimore school of medicine",
        "university of maryland at baltimore school of medicine",
        "university of maryland som",
        "umd school of medicine",
    ],
}


# =====================================================================
# HTTP session
# =====================================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": (
            "HolmLabPublicationsBot/3.0 "
            "(mailto:jholm@som.umaryland.edu)"
        )
    }
)


# =====================================================================
# General helper functions
# =====================================================================

def clean(value: Any) -> str:
    """Convert a value to clean, single-spaced text."""

    return re.sub(
        r"\s+",
        " ",
        str(value or ""),
    ).strip()


def clean_orcid(value: Any) -> str:
    """Normalize an ORCID URL or identifier."""

    orcid = clean(value)

    orcid = re.sub(
        r"^https?://orcid\.org/",
        "",
        orcid,
        flags=re.IGNORECASE,
    )

    return orcid.strip().strip("/")


def normalize_doi(value: Any) -> str:
    """Normalize a DOI."""

    doi = clean(value).lower()

    for prefix in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
            break

    return doi.strip().strip("/")


def normalize_pmid(value: Any) -> str:
    """Normalize a PubMed identifier."""

    pmid = clean(value)

    pmid = re.sub(
        r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/",
        "",
        pmid,
        flags=re.IGNORECASE,
    )

    return pmid.strip().strip("/")


def normalize_pmc(value: Any) -> str:
    """Normalize a PubMed Central identifier."""

    pmc = clean(value)

    pmc = re.sub(
        r"^https?://www\.ncbi\.nlm\.nih\.gov/pmc/articles/",
        "",
        pmc,
        flags=re.IGNORECASE,
    )

    return pmc.strip().strip("/")


def normalize_title(value: Any) -> str:
    """Normalize a publication title for duplicate matching."""

    title = clean(value).lower()

    title = re.sub(
        r"<[^>]+>",
        " ",
        title,
    )

    title = re.sub(
        r"&[a-z0-9#]+;",
        " ",
        title,
    )

    title = re.sub(
        r"[^a-z0-9]+",
        "",
        title,
    )

    return title


def normalize_name(value: Any) -> str:
    """Normalize a person's name."""

    name = clean(value).lower()

    name = re.sub(
        r"[^a-z0-9]+",
        " ",
        name,
    )

    return clean(name)


def normalize_affiliation(value: Any) -> str:
    """Normalize an institutional affiliation."""

    affiliation = clean(value).lower()

    affiliation = affiliation.replace(
        "&",
        " and ",
    )

    affiliation = re.sub(
        r"[^a-z0-9]+",
        " ",
        affiliation,
    )

    return clean(affiliation)


def year_from(value: Any) -> int | None:
    """Extract a four-digit publication year."""

    match = re.search(
        r"\b(?:19|20)\d{2}\b",
        clean(value),
    )

    if not match:
        return None

    return int(match.group(0))


def first_text(
    node: ET.Element | None,
    path: str,
    default: str = "",
) -> str:
    """Return text from the first matching XML element."""

    if node is None:
        return default

    child = node.find(path)

    if child is None:
        return default

    return clean(
        "".join(
            child.itertext()
        )
    )


def first_nonempty(*values: Any) -> str:
    """Return the first nonempty value."""

    for value in values:
        text = clean(value)

        if text:
            return text

    return ""


def make_pages(
    first_page: Any,
    last_page: Any,
) -> str:
    """Create a publication page range."""

    first = clean(first_page)
    last = clean(last_page)

    if first and last and first != last:
        return f"{first}-{last}"

    return first or last


def unique_strings(values: list[Any]) -> list[str]:
    """Return sorted unique nonempty strings."""

    return sorted(
        {
            clean(value)
            for value in values
            if clean(value)
        }
    )


# =====================================================================
# Target-author matching
# =====================================================================

def is_target_author_name(
    name: Any,
    author_config: dict[str, Any],
) -> bool:
    """Determine whether a name belongs to Johanna Holm."""

    normalized = normalize_name(name)

    if not normalized:
        return False

    configured_names = [
        author_config.get("name"),
        author_config.get("display_name"),
        "Johanna B. Holm",
        "Johanna Holm",
        "Johanna B Holm",
        "Holm JB",
        "Holm J B",
    ]

    normalized_candidates = {
        normalize_name(candidate)
        for candidate in configured_names
        if normalize_name(candidate)
    }

    if normalized in normalized_candidates:
        return True

    tokens = normalized.split()

    if "holm" not in tokens:
        return False

    has_johanna = "johanna" in tokens
    has_j_initial = "j" in tokens or "jb" in tokens

    return has_johanna or has_j_initial


def orcid_matches(
    first: Any,
    second: Any,
) -> bool:
    """Compare two ORCID values."""

    first_orcid = clean_orcid(first)
    second_orcid = clean_orcid(second)

    return bool(
        first_orcid
        and second_orcid
        and first_orcid == second_orcid
    )


# =====================================================================
# Affiliation matching
# =====================================================================

def affiliation_matches(
    affiliation: Any,
    allowed_affiliations: list[str],
) -> bool:
    """Check whether an affiliation matches the allowlist."""

    normalized_affiliation = normalize_affiliation(
        affiliation
    )

    if not normalized_affiliation:
        return False

    for allowed in allowed_affiliations:
        normalized_allowed = normalize_affiliation(
            allowed
        )

        aliases = AFFILIATION_ALIASES.get(
            normalized_allowed,
            [normalized_allowed],
        )

        for alias in aliases:
            normalized_alias = normalize_affiliation(
                alias
            )

            if (
                normalized_alias
                and normalized_alias in normalized_affiliation
            ):
                return True

    return False


def matching_affiliations(
    affiliations: list[Any],
    allowed_affiliations: list[str],
) -> list[str]:
    """Return affiliations that match the institutional allowlist."""

    return unique_strings(
        [
            affiliation
            for affiliation in affiliations
            if affiliation_matches(
                affiliation,
                allowed_affiliations,
            )
        ]
    )


def record_has_allowed_affiliation(
    record: dict[str, Any],
    allowed_affiliations: list[str],
) -> bool:
    """Check whether the target author's affiliation is approved."""

    target_affiliations = (
        record.get("affiliations")
        or []
    )

    return any(
        affiliation_matches(
            affiliation,
            allowed_affiliations,
        )
        for affiliation in target_affiliations
    )


# =====================================================================
# HTTP request helper
# =====================================================================

def request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: dict[str, Any] | None = None,
) -> Any:
    """Make a JSON request with retry handling."""

    last_error: Exception | None = None

    for attempt in range(4):
        try:
            response = SESSION.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                data=data,
                timeout=60,
            )

            if (
                response.status_code == 429
                or response.status_code >= 500
            ):
                wait_seconds = 2 ** attempt

                print(
                    f"Temporary API response "
                    f"{response.status_code} from {url}. "
                    f"Retrying in {wait_seconds} seconds."
                )

                time.sleep(
                    wait_seconds
                )

                continue

            response.raise_for_status()

            return response.json()

        except requests.HTTPError as exc:
            last_error = exc

            status_code = (
                exc.response.status_code
                if exc.response is not None
                else None
            )

            if (
                status_code is not None
                and 400 <= status_code < 500
                and status_code != 429
            ):
                response_text = clean(
                    exc.response.text
                    if exc.response is not None
                    else ""
                )

                raise RuntimeError(
                    f"API request rejected by {url} "
                    f"with status {status_code}. "
                    f"Response: {response_text[:500]}"
                ) from exc

            if attempt == 3:
                break

            wait_seconds = 2 ** attempt

            print(
                f"Request failed for {url}: {exc}. "
                f"Retrying in {wait_seconds} seconds."
            )

            time.sleep(
                wait_seconds
            )

        except requests.RequestException as exc:
            last_error = exc

            if attempt == 3:
                break

            wait_seconds = 2 ** attempt

            print(
                f"Request failed for {url}: {exc}. "
                f"Retrying in {wait_seconds} seconds."
            )

            time.sleep(
                wait_seconds
            )

    raise RuntimeError(
        f"Repeated request failure for {url}: "
        f"{last_error}"
    )


# =====================================================================
# PubMed
# =====================================================================

def pubmed_ids(
    query: str,
    email: str,
    api_key: str = "",
) -> list[str]:
    """Find PubMed records matching the configured author query."""

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

    identifiers = (
        data.get(
            "esearchresult",
            {},
        ).get(
            "idlist",
            [],
        )
    )

    print(
        f'PubMed query "{query}" returned '
        f"{len(identifiers)} records."
    )

    return identifiers


def extract_pubmed_author_data(
    journal_article: ET.Element | None,
    author_config: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """
    Extract all author names and only the target author's affiliations.
    """

    authors: list[str] = []
    target_affiliations: list[str] = []

    if journal_article is None:
        return authors, target_affiliations

    for author in journal_article.findall(
        "AuthorList/Author"
    ):
        collective_name = first_text(
            author,
            "CollectiveName",
        )

        if collective_name:
            authors.append(
                collective_name
            )

            continue

        fore_name = first_text(
            author,
            "ForeName",
        )

        initials = first_text(
            author,
            "Initials",
        )

        last_name = first_text(
            author,
            "LastName",
        )

        full_name = " ".join(
            filter(
                None,
                [
                    fore_name or initials,
                    last_name,
                ],
            )
        )

        if full_name:
            authors.append(
                full_name
            )

        possible_names = [
            full_name,
            f"{last_name} {initials}",
            f"{last_name} {fore_name}",
        ]

        is_target = any(
            is_target_author_name(
                possible_name,
                author_config,
            )
            for possible_name in possible_names
        )

        if not is_target:
            continue

        for affiliation_node in author.findall(
            "AffiliationInfo/Affiliation"
        ):
            affiliation = clean(
                "".join(
                    affiliation_node.itertext()
                )
            )

            if affiliation:
                target_affiliations.append(
                    affiliation
                )

    return (
        unique_strings(authors),
        unique_strings(target_affiliations),
    )


def fetch_pubmed(
    ids: list[str],
    email: str,
    author_config: dict[str, Any],
    api_key: str = "",
) -> list[dict[str, Any]]:
    """Retrieve and normalize PubMed records."""

    records: list[dict[str, Any]] = []

    for start in range(
        0,
        len(ids),
        200,
    ):
        batch = ids[
            start:start + 200
        ]

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

        root = ET.fromstring(
            response.content
        )

        for pubmed_article in root.findall(
            ".//PubmedArticle"
        ):
            citation = pubmed_article.find(
                "MedlineCitation"
            )

            journal_article = (
                citation.find("Article")
                if citation is not None
                else None
            )

            pmid = normalize_pmid(
                first_text(
                    citation,
                    "PMID",
                )
            )

            title = first_text(
                journal_article,
                "ArticleTitle",
            )

            journal = first_text(
                journal_article,
                "Journal/Title",
            )

            authors, target_affiliations = (
                extract_pubmed_author_data(
                    journal_article,
                    author_config,
                )
            )

            doi = ""
            pmc = ""

            for article_id in pubmed_article.findall(
                "PubmedData/ArticleIdList/ArticleId"
            ):
                identifier_type = (
                    article_id.attrib.get(
                        "IdType",
                        "",
                    ).lower()
                )

                if identifier_type == "doi":
                    doi = normalize_doi(
                        article_id.text
                    )

                elif identifier_type == "pmc":
                    pmc = normalize_pmc(
                        article_id.text
                    )

            publication_date_node = (
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
                        first_text(
                            publication_date_node,
                            "Year",
                        ),
                        first_text(
                            publication_date_node,
                            "Month",
                        ),
                        first_text(
                            publication_date_node,
                            "Day",
                        ),
                        first_text(
                            publication_date_node,
                            "MedlineDate",
                        ),
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

            if (
                not pages
                and journal_article is not None
            ):
                for location in journal_article.findall(
                    "ELocationID"
                ):
                    location_type = (
                        location.attrib.get(
                            "EIdType",
                            "",
                        ).lower()
                    )

                    if location_type != "doi":
                        pages = clean(
                            location.text
                        )

                        if pages:
                            break

            records.append(
                {
                    "title": title,
                    "authors": authors,
                    "affiliations": target_affiliations,
                    "journal": journal,
                    "year": year_from(
                        date_text
                    ),
                    "date": date_text,
                    "volume": volume,
                    "issue": issue,
                    "pages": pages,
                    "doi": doi,
                    "pmid": pmid,
                    "pmc": pmc,
                    "url": (
                        f"https://pubmed.ncbi.nlm.nih.gov/"
                        f"{pmid}/"
                        if pmid
                        else ""
                    ),
                    "type": "journal-article",
                    "sources": [
                        "PubMed"
                    ],
                }
            )

    print(
        f"PubMed supplied "
        f"{len(records)} normalized records."
    )

    return records


# =====================================================================
# Crossref
# =====================================================================

def extract_crossref_author_data(
    item: dict[str, Any],
    author_config: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """
    Extract all author names and only the target author's affiliations.
    """

    authors: list[str] = []
    target_affiliations: list[str] = []

    configured_orcid = clean_orcid(
        author_config.get("orcid")
    )

    for author in item.get(
        "author",
        [],
    ):
        given = clean(
            author.get("given")
        )

        family = clean(
            author.get("family")
        )

        author_name = " ".join(
            filter(
                None,
                [
                    given,
                    family,
                ],
            )
        )

        if author_name:
            authors.append(
                author_name
            )

        author_orcid = clean_orcid(
            author.get("ORCID")
            or author.get("orcid")
        )

        is_target = (
            orcid_matches(
                author_orcid,
                configured_orcid,
            )
            or is_target_author_name(
                author_name,
                author_config,
            )
        )

        if not is_target:
            continue

        for affiliation_item in (
            author.get("affiliation")
            or []
        ):
            affiliation = clean(
                affiliation_item.get(
                    "name"
                )
            )

            if affiliation:
                target_affiliations.append(
                    affiliation
                )

    return (
        unique_strings(authors),
        unique_strings(target_affiliations),
    )


def crossref_by_orcid(
    orcid: str,
    email: str,
    include_types: set[str],
    author_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Retrieve Crossref publications linked to an ORCID."""

    orcid = clean_orcid(
        orcid
    )

    if (
        not orcid
        or "REPLACE_" in orcid
    ):
        return []

    cursor = "*"
    rows = 1000

    records: list[dict[str, Any]] = []

    while cursor:
        params = {
            "filter": f"orcid:{orcid}",
            "rows": rows,
            "cursor": cursor,
            "mailto": email,
        }

        data = request_json(
            f"{CROSSREF_BASE}/works",
            params=params,
        )

        message = data.get(
            "message",
            {},
        )

        items = message.get(
            "items",
            [],
        )

        for item in items:
            item_type = clean(
                item.get("type")
            ).lower()

            if (
                include_types
                and item_type not in include_types
            ):
                continue

            authors, target_affiliations = (
                extract_crossref_author_data(
                    item,
                    author_config,
                )
            )

            date_parts = (
                item.get(
                    "published-print",
                    {},
                ).get(
                    "date-parts"
                )
                or item.get(
                    "published-online",
                    {},
                ).get(
                    "date-parts"
                )
                or item.get(
                    "issued",
                    {},
                ).get(
                    "date-parts"
                )
                or []
            )

            year = (
                date_parts[0][0]
                if (
                    date_parts
                    and date_parts[0]
                )
                else None
            )

            doi = normalize_doi(
                item.get("DOI")
            )

            title_values = (
                item.get("title")
                or [""]
            )

            journal_values = (
                item.get("container-title")
                or [""]
            )

            records.append(
                {
                    "title": clean(
                        title_values[0]
                    ),
                    "authors": authors,
                    "affiliations": target_affiliations,
                    "journal": clean(
                        journal_values[0]
                    ),
                    "year": year,
                    "date": str(
                        year or ""
                    ),
                    "volume": clean(
                        item.get("volume")
                    ),
                    "issue": clean(
                        item.get("issue")
                    ),
                    "pages": clean(
                        item.get("page")
                        or item.get(
                            "article-number"
                        )
                    ),
                    "doi": doi,
                    "pmid": "",
                    "pmc": "",
                    "url": (
                        f"https://doi.org/{doi}"
                        if doi
                        else clean(
                            item.get("URL")
                        )
                    ),
                    "type": item_type,
                    "sources": [
                        "Crossref"
                    ],
                }
            )

        print(
            f"Crossref retrieved {len(items)} records "
            f"in this page for ORCID {orcid}."
        )

        if len(items) < rows:
            break

        next_cursor = message.get(
            "next-cursor"
        )

        if (
            not next_cursor
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
    """Get an ORCID API token when credentials are available."""

    client_id = os.getenv(
        "ORCID_CLIENT_ID",
        "",
    ).strip()

    client_secret = os.getenv(
        "ORCID_CLIENT_SECRET",
        "",
    ).strip()

    if (
        not client_id
        or not client_secret
    ):
        print(
            "ORCID credentials are missing. "
            "Direct ORCID retrieval will be skipped."
        )

        return ""

    data = request_json(
        ORCID_TOKEN_URL,
        method="POST",
        headers={
            "Accept": "application/json"
        },
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "/read-public",
        },
    )

    return clean(
        data.get("access_token")
    )


def orcid_works(
    orcid: str,
    token: str,
) -> list[dict[str, Any]]:
    """
    Retrieve ORCID work summaries.

    ORCID summary records generally do not provide reliable
    publication-level author affiliations. Their records therefore have
    an empty affiliation list and must merge with PubMed, Crossref, or
    OpenAlex records to pass the final affiliation filter.
    """

    orcid = clean_orcid(
        orcid
    )

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
            "Authorization": (
                f"Bearer {token}"
            ),
        },
    )

    records: list[dict[str, Any]] = []

    for group in data.get(
        "group",
        [],
    ):
        summaries = group.get(
            "work-summary",
            [],
        )

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
                external_id.get(
                    "external-id-type"
                )
            ).lower()

            identifier_value = clean(
                external_id.get(
                    "external-id-value"
                )
            )

            if identifier_type:
                external_ids[
                    identifier_type
                ] = identifier_value

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
            summary.get(
                "publication-date"
            )
            or {}
        )

        year = year_from(
            (
                publication_date.get(
                    "year"
                )
                or {}
            ).get(
                "value"
            )
        )

        records.append(
            {
                "title": title,
                "authors": [],
                "affiliations": [],
                "journal": clean(
                    (
                        summary.get(
                            "journal-title"
                        )
                        or {}
                    ).get(
                        "value"
                    )
                ),
                "year": year,
                "date": str(
                    year or ""
                ),
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
                        f"https://pubmed.ncbi.nlm.nih.gov/"
                        f"{pmid}/"
                        if pmid
                        else ""
                    )
                ),
                "type": clean(
                    summary.get("type")
                ).lower(),
                "sources": [
                    "ORCID"
                ],
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
    """Resolve an ORCID to an OpenAlex author."""

    orcid = clean_orcid(
        orcid
    )

    if (
        not orcid
        or "REPLACE_" in orcid
    ):
        return None

    params: dict[str, Any] = {
        "filter": f"orcid:{orcid}",
        "per_page": 10,
    }

    if api_key:
        params["api_key"] = api_key

    data = request_json(
        f"{OPENALEX_BASE}/authors",
        params=params,
    )

    results = data.get(
        "results",
        [],
    )

    if not results:
        print(
            f"OpenAlex could not find an author "
            f"for ORCID {orcid}."
        )

        return None

    return results[0]


def get_openalex_source(
    work: dict[str, Any],
) -> dict[str, Any]:
    """Return the best available journal or source."""

    primary_location = (
        work.get(
            "primary_location"
        )
        or {}
    )

    source = (
        primary_location.get(
            "source"
        )
        or {}
    )

    if source:
        return source

    best_location = (
        work.get(
            "best_oa_location"
        )
        or {}
    )

    source = (
        best_location.get(
            "source"
        )
        or {}
    )

    if source:
        return source

    for location in (
        work.get("locations")
        or []
    ):
        source = (
            location.get("source")
            or {}
        )

        if source:
            return source

    return {}


def get_openalex_full_text_url(
    work: dict[str, Any],
) -> str:
    """Return the best available open-access URL."""

    best_location = (
        work.get(
            "best_oa_location"
        )
        or {}
    )

    full_text_url = first_nonempty(
        best_location.get(
            "pdf_url"
        ),
        best_location.get(
            "landing_page_url"
        ),
    )

    if full_text_url:
        return full_text_url

    primary_location = (
        work.get(
            "primary_location"
        )
        or {}
    )

    return first_nonempty(
        primary_location.get(
            "pdf_url"
        ),
        primary_location.get(
            "landing_page_url"
        ),
    )


def normalize_openalex_work(
    work: dict[str, Any],
    target_author_id: str,
) -> dict[str, Any] | None:
    """
    Normalize an OpenAlex work.

    Only institutions attached to the target author's authorship are
    saved in the affiliations field.
    """

    title = clean(
        work.get("title")
        or work.get(
            "display_name"
        )
    )

    if not title:
        return None

    authors: list[str] = []
    target_affiliations: list[str] = []

    normalized_target_id = clean(
        target_author_id
    ).rstrip(
        "/"
    ).split(
        "/"
    )[-1]

    for authorship in (
        work.get("authorships")
        or []
    ):
        author_data = (
            authorship.get("author")
            or {}
        )

        author_name = clean(
            author_data.get(
                "display_name"
            )
        )

        if author_name:
            authors.append(
                author_name
            )

        authorship_author_id = clean(
            author_data.get("id")
        ).rstrip(
            "/"
        ).split(
            "/"
        )[-1]

        if (
            authorship_author_id
            != normalized_target_id
        ):
            continue

        for institution in (
            authorship.get("institutions")
            or []
        ):
            institution_name = clean(
                institution.get(
                    "display_name"
                )
            )

            if institution_name:
                target_affiliations.append(
                    institution_name
                )

        raw_affiliation_strings = (
            authorship.get(
                "raw_affiliation_strings"
            )
            or []
        )

        for affiliation in raw_affiliation_strings:
            if clean(affiliation):
                target_affiliations.append(
                    clean(affiliation)
                )

    source = get_openalex_source(
        work
    )

    bibliography = (
        work.get("biblio")
        or {}
    )

    identifiers = (
        work.get("ids")
        or {}
    )

    doi = normalize_doi(
        work.get("doi")
    )

    pmid = normalize_pmid(
        identifiers.get("pmid")
    )

    pmc = normalize_pmc(
        identifiers.get("pmcid")
        or identifiers.get("pmc")
    )

    publication_date = clean(
        work.get(
            "publication_date"
        )
    )

    publication_year = (
        work.get(
            "publication_year"
        )
        or year_from(
            publication_date
        )
    )

    primary_location = (
        work.get(
            "primary_location"
        )
        or {}
    )

    landing_page_url = clean(
        primary_location.get(
            "landing_page_url"
        )
    )

    openalex_url = clean(
        work.get("id")
    )

    full_text_url = (
        get_openalex_full_text_url(
            work
        )
    )

    if doi:
        url = (
            f"https://doi.org/{doi}"
        )

    elif pmid:
        url = (
            f"https://pubmed.ncbi.nlm.nih.gov/"
            f"{pmid}/"
        )

    elif landing_page_url:
        url = landing_page_url

    else:
        url = openalex_url

    return {
        "title": title,
        "authors": unique_strings(
            authors
        ),
        "affiliations": unique_strings(
            target_affiliations
        ),
        "journal": clean(
            source.get(
                "display_name"
            )
        ),
        "year": publication_year,
        "date": (
            publication_date
            or str(
                publication_year
                or ""
            )
        ),
        "volume": clean(
            bibliography.get(
                "volume"
            )
        ),
        "issue": clean(
            bibliography.get(
                "issue"
            )
        ),
        "pages": make_pages(
            bibliography.get(
                "first_page"
            ),
            bibliography.get(
                "last_page"
            ),
        ),
        "doi": doi,
        "pmid": pmid,
        "pmc": pmc,
        "url": url,
        "full_text_url": full_text_url,
        "type": clean(
            work.get("type")
        ).lower(),
        "openalex_id": openalex_url,
        "sources": [
            "OpenAlex"
        ],
    }


def openalex_works(
    orcid: str,
    api_key: str,
    include_types: set[str],
) -> list[dict[str, Any]]:
    """Retrieve OpenAlex works for the ORCID-linked author."""

    orcid = clean_orcid(
        orcid
    )

    if (
        not orcid
        or "REPLACE_" in orcid
    ):
        return []

    author = get_openalex_author(
        orcid,
        api_key,
    )

    if not author:
        return []

    full_author_id = clean(
        author.get("id")
    )

    author_id = full_author_id.rstrip(
        "/"
    ).split(
        "/"
    )[-1]

    author_name = clean(
        author.get(
            "display_name"
        )
    )

    if not author_id:
        print(
            f"OpenAlex returned no author ID "
            f"for ORCID {orcid}."
        )

        return []

    print(
        f"OpenAlex author resolved: "
        f"{author_name or 'Unknown author'} "
        f"({author_id})."
    )

    type_aliases = {
        "article": "journal-article",
        "book": "book",
        "book-chapter": "book-chapter",
        "dataset": "dataset",
        "dissertation": "dissertation",
        "editorial": "editorial",
        "letter": "letter",
        "preprint": "posted-content",
        "report": "report",
        "review": "journal-article",
    }

    records: list[dict[str, Any]] = []
    cursor = "*"

    while cursor:
        params: dict[str, Any] = {
            "filter": (
                f"authorships.author.id:{author_id}"
            ),
            "sort": "publication_date:desc",
            "per_page": 100,
            "cursor": cursor,
        }

        if api_key:
            params["api_key"] = api_key

        data = request_json(
            f"{OPENALEX_BASE}/works",
            params=params,
        )

        results = data.get(
            "results",
            [],
        )

        for work in results:
            record = normalize_openalex_work(
                work,
                author_id,
            )

            if not record:
                continue

            record_type = clean(
                record.get("type")
            ).lower()

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

            records.append(
                record
            )

        next_cursor = (
            data.get(
                "meta",
                {},
            ).get(
                "next_cursor"
            )
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
# Record merging
# =====================================================================

def record_quality(
    record: dict[str, Any],
) -> int:
    """Score publication metadata completeness."""

    field_weights = {
        "doi": 6,
        "pmid": 6,
        "title": 5,
        "authors": 5,
        "affiliations": 5,
        "journal": 4,
        "year": 4,
        "date": 2,
        "volume": 2,
        "issue": 2,
        "pages": 2,
        "url": 1,
        "full_text_url": 1,
    }

    return sum(
        weight
        for field, weight in field_weights.items()
        if record.get(field)
    )


def records_match(
    first: dict[str, Any],
    second: dict[str, Any],
) -> bool:
    """Determine whether two records represent the same publication."""

    first_doi = normalize_doi(
        first.get("doi")
    )

    second_doi = normalize_doi(
        second.get("doi")
    )

    if (
        first_doi
        and second_doi
        and first_doi == second_doi
    ):
        return True

    first_pmid = normalize_pmid(
        first.get("pmid")
    )

    second_pmid = normalize_pmid(
        second.get("pmid")
    )

    if (
        first_pmid
        and second_pmid
        and first_pmid == second_pmid
    ):
        return True

    first_title = normalize_title(
        first.get("title")
    )

    second_title = normalize_title(
        second.get("title")
    )

    if (
        first_title
        and second_title
        and first_title == second_title
    ):
        first_year = first.get(
            "year"
        )

        second_year = second.get(
            "year"
        )

        if (
            not first_year
            or not second_year
            or int(first_year) == int(second_year)
        ):
            return True

    return False


def combine_records(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    """Combine duplicate publication records."""

    if (
        record_quality(second)
        > record_quality(first)
    ):
        first, second = second, first

    merged = dict(
        first
    )

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
        "type",
        "openalex_id",
    )

    for field in scalar_fields:
        if (
            not merged.get(field)
            and second.get(field)
        ):
            merged[field] = (
                second[field]
            )

    if (
        not merged.get("year")
        and second.get("year")
    ):
        merged["year"] = (
            second["year"]
        )

    first_authors = (
        merged.get("authors")
        or []
    )

    second_authors = (
        second.get("authors")
        or []
    )

    if (
        not first_authors
        or len(second_authors)
        > len(first_authors)
    ):
        merged["authors"] = (
            second_authors
        )

    merged["affiliations"] = unique_strings(
        (
            merged.get("affiliations")
            or []
        )
        + (
            second.get("affiliations")
            or []
        )
    )

    merged["sources"] = unique_strings(
        (
            merged.get("sources")
            or []
        )
        + (
            second.get("sources")
            or []
        )
    )

    return merged


def merge_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Deduplicate publications."""

    merged: list[dict[str, Any]] = []

    for record in records:
        matching_index: int | None = None

        for index, existing in enumerate(
            merged
        ):
            if records_match(
                existing,
                record,
            ):
                matching_index = index
                break

        if matching_index is None:
            merged.append(
                dict(record)
            )

        else:
            merged[matching_index] = (
                combine_records(
                    merged[matching_index],
                    record,
                )
            )

    return merged


# =====================================================================
# Review and source-failure helpers
# =====================================================================

def add_review_record(
    review: list[dict[str, Any]],
    reason: str,
    record: dict[str, Any],
) -> None:
    """Add a unique item to publication-review.json."""

    review_key = (
        reason,
        normalize_doi(
            record.get("doi")
        ),
        normalize_pmid(
            record.get("pmid")
        ),
        normalize_title(
            record.get("title")
        ),
        record.get("year"),
    )

    for existing in review:
        existing_record = (
            existing.get("record")
            or {}
        )

        existing_key = (
            existing.get("reason"),
            normalize_doi(
                existing_record.get(
                    "doi"
                )
            ),
            normalize_pmid(
                existing_record.get(
                    "pmid"
                )
            ),
            normalize_title(
                existing_record.get(
                    "title"
                )
            ),
            existing_record.get(
                "year"
            ),
        )

        if existing_key == review_key:
            return

    review.append(
        {
            "reason": reason,
            "record": record,
        }
    )


def safe_source_call(
    source_name: str,
    function: Callable[..., list[dict[str, Any]]],
    review: list[dict[str, Any]],
    *args: Any,
) -> list[dict[str, Any]]:
    """Run one source without stopping the entire workflow."""

    try:
        return function(
            *args
        )

    except Exception as exc:
        message = (
            f"{source_name} retrieval failed: "
            f"{exc}"
        )

        print(
            f"WARNING: {message}",
            file=sys.stderr,
        )

        review.append(
            {
                "reason": message,
                "record": {},
            }
        )

        return []


# =====================================================================
# Final record cleanup
# =====================================================================

def clean_public_record(
    publication: dict[str, Any],
    allowed_affiliations: list[str],
    updated_at: str,
) -> dict[str, Any]:
    """Prepare a publication for the public JSON file."""

    publication = dict(
        publication
    )

    doi = normalize_doi(
        publication.get("doi")
    )

    pmid = normalize_pmid(
        publication.get("pmid")
    )

    pmc = normalize_pmc(
        publication.get("pmc")
    )

    publication["doi"] = doi
    publication["pmid"] = pmid
    publication["pmc"] = pmc

    publication["authors"] = unique_strings(
        publication.get("authors")
        or []
    )

    publication["affiliations"] = (
        matching_affiliations(
            publication.get("affiliations")
            or [],
            allowed_affiliations,
        )
    )

    publication["sources"] = unique_strings(
        publication.get("sources")
        or []
    )

    title_key = normalize_title(
        publication.get("title")
    )

    identifier_text = (
        doi
        or pmid
        or (
            f"{title_key}:"
            f"{publication.get('year') or ''}"
        )
    )

    publication["id"] = (
        hashlib.sha1(
            identifier_text.encode(
                "utf-8"
            )
        ).hexdigest()[:12]
    )

    publication["updated_at"] = updated_at

    # Abstracts are never included in the public JSON.
    publication.pop(
        "abstract",
        None,
    )

    publication.pop(
        "abstract_inverted_index",
        None,
    )

    return publication


# =====================================================================
# Main pipeline
# =====================================================================

def main() -> int:
    """Run the publication update pipeline."""

    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Could not find config.json at "
            f"{CONFIG_PATH}"
        )

    config = json.loads(
        CONFIG_PATH.read_text(
            encoding="utf-8"
        )
    )

    email = clean(
        config.get(
            "contact_email"
        )
    )

    if (
        not email
        or "YOUR_EMAIL" in email
    ):
        raise RuntimeError(
            "Replace contact_email in config.json "
            "with a real email address."
        )

    ncbi_api_key = os.getenv(
        "NCBI_API_KEY",
        "",
    ).strip()

    openalex_api_key = os.getenv(
        "OPENALEX_API_KEY",
        "",
    ).strip()

    include_types = {
        clean(
            publication_type
        ).lower()
        for publication_type in config.get(
            "include_types",
            [],
        )
        if clean(
            publication_type
        )
    }

    allowed_affiliations = [
        clean(
            affiliation
        )
        for affiliation in config.get(
            "allowed_affiliations",
            DEFAULT_ALLOWED_AFFILIATIONS,
        )
        if clean(
            affiliation
        )
    ]

    print(
        "Allowed target-author affiliations:"
    )

    for affiliation in allowed_affiliations:
        print(
            f"  - {affiliation}"
        )

    all_records: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []

    orcid_token = get_orcid_token()

    authors_config = config.get(
        "authors",
        [],
    )

    if not authors_config:
        raise RuntimeError(
            "No authors are configured "
            "in config.json."
        )

    for author in authors_config:
        query = clean(
            author.get(
                "pubmed_query"
            )
        )

        orcid = clean_orcid(
            author.get("orcid")
        )

        author_name = first_nonempty(
            author.get("name"),
            author.get(
                "display_name"
            ),
            orcid,
            query,
        )

        print()
        print(
            f"Retrieving publications for "
            f"{author_name or 'configured author'}..."
        )

        if query:
            try:
                identifiers = pubmed_ids(
                    query,
                    email,
                    ncbi_api_key,
                )

                pubmed_records = fetch_pubmed(
                    identifiers,
                    email,
                    author,
                    ncbi_api_key,
                )

                all_records.extend(
                    pubmed_records
                )

            except Exception as exc:
                message = (
                    f"PubMed retrieval failed: "
                    f"{exc}"
                )

                print(
                    f"WARNING: {message}",
                    file=sys.stderr,
                )

                review.append(
                    {
                        "reason": message,
                        "record": {},
                    }
                )

        if (
            orcid
            and "REPLACE_" not in orcid
        ):
            crossref_records = safe_source_call(
                "Crossref",
                crossref_by_orcid,
                review,
                orcid,
                email,
                include_types,
                author,
            )

            all_records.extend(
                crossref_records
            )

            direct_orcid_records = safe_source_call(
                "ORCID",
                orcid_works,
                review,
                orcid,
                orcid_token,
            )

            all_records.extend(
                direct_orcid_records
            )

            openalex_records = safe_source_call(
                "OpenAlex",
                openalex_works,
                review,
                orcid,
                openalex_api_key,
                include_types,
            )

            all_records.extend(
                openalex_records
            )

        elif not query:
            print(
                "This author has neither a PubMed query "
                "nor a valid ORCID."
            )

    print()
    print(
        f"Retrieved {len(all_records)} total records "
        f"before deduplication."
    )

    publications = merge_records(
        all_records
    )

    print(
        f"{len(publications)} unique records remain "
        f"after deduplication."
    )

    earliest_year = int(
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

    updated_at = datetime.now(
        timezone.utc
    ).isoformat()

    excluded_by_affiliation = 0

    for publication in publications:
        publication_year = (
            publication.get("year")
        )

        if (
            publication_year
            and int(publication_year) < earliest_year
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

        if (
            doi
            and doi in excluded_dois
        ):
            continue

        if (
            pmid
            and pmid in excluded_pmids
        ):
            continue

        if (
            title_key
            and title_key in excluded_titles
        ):
            continue

        if not publication.get(
            "title"
        ):
            add_review_record(
                review,
                "Missing title",
                publication,
            )

            continue

        if not publication.get(
            "year"
        ):
            add_review_record(
                review,
                "Missing publication year",
                publication,
            )

        if not record_has_allowed_affiliation(
            publication,
            allowed_affiliations,
        ):
            excluded_by_affiliation += 1

            add_review_record(
                review,
                (
                    "Excluded because the target author's "
                    "affiliation did not match the institutional allowlist"
                ),
                publication,
            )

            continue

        public_record = clean_public_record(
            publication,
            allowed_affiliations,
            updated_at,
        )

        final.append(
            public_record
        )

    final.sort(
        key=lambda publication: (
            -(
                publication.get(
                    "year"
                )
                or 0
            ),
            clean(
                publication.get(
                    "title"
                )
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
        f"Excluded {excluded_by_affiliation} records "
        f"because Johanna's affiliation did not match "
        f"the approved institutions."
    )

    print(
        f"Wrote {len(final)} publications "
        f"to {OUTPUT_PATH}."
    )

    print(
        f"Wrote {len(review)} review entries "
        f"to {REVIEW_PATH}."
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
