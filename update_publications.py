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
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents
CONFIG_PATH = ROOT / "config.json"
OUTPUT_PATH = ROOT / "publications.json"
REVIEW_PATH = ROOT / "publication-review.json"

NCBI_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
CROSSREF_BASE = "https://api.crossref.org"
ORCID_BASE = "https://pub.orcid.org/v3.0"
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "LabPublicationsBot/1.0"})


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_doi(value: Any) -> str:
    doi = clean(value).lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.strip()


def normalize_title(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean(value).lower())


def year_from(value: Any) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", clean(value))
    return int(match.group(0)) if match else None


def first_text(node: ET.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    child = node.find(path)
    return clean("".join(child.itertext())) if child is not None else default


def request_json(url: str, *, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None,
                 method: str = "GET", data: dict[str, Any] | None = None) -> Any:
    for attempt in range(4):
        response = SESSION.request(
            method, url, params=params, headers=headers, data=data, timeout=45
        )
        if response.status_code == 429 or response.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Repeated request failure: {url}")


def pubmed_ids(query: str, email: str, api_key: str = "") -> list[str]:
    params = {
        "db": "pubmed", "term": query, "retmode": "json",
        "retmax": 10000, "tool": "lab_publications", "email": email
    }
    if api_key:
        params["api_key"] = api_key
    data = request_json(f"{NCBI_BASE}/esearch.fcgi", params=params)
    return data.get("esearchresult", {}).get("idlist", [])


def fetch_pubmed(ids: list[str], email: str, api_key: str = "") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for start in range(0, len(ids), 200):
        params = {
            "db": "pubmed", "id": ",".join(ids[start:start + 200]),
            "retmode": "xml", "tool": "lab_publications", "email": email
        }
        if api_key:
            params["api_key"] = api_key
        response = SESSION.get(f"{NCBI_BASE}/efetch.fcgi", params=params, timeout=60)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        for article in root.findall(".//PubmedArticle"):
            citation = article.find("MedlineCitation")
            journal_article = citation.find("Article") if citation is not None else None
            pmid = first_text(citation, "PMID")
            title = first_text(journal_article, "ArticleTitle")
            journal = first_text(journal_article, "Journal/Title")
            abstract_parts = [
                clean("".join(x.itertext()))
                for x in journal_article.findall("Abstract/AbstractText")
            ] if journal_article is not None else []

            authors = []
            if journal_article is not None:
                for author in journal_article.findall("AuthorList/Author"):
                    collective = first_text(author, "CollectiveName")
                    if collective:
                        authors.append(collective)
                    else:
                        name = " ".join(filter(None, [
                            first_text(author, "ForeName"),
                            first_text(author, "LastName")
                        ]))
                        if name:
                            authors.append(name)

            doi = ""
            pmc = ""
            for aid in article.findall("PubmedData/ArticleIdList/ArticleId"):
                kind = aid.attrib.get("IdType", "")
                if kind == "doi":
                    doi = normalize_doi(aid.text)
                elif kind == "pmc":
                    pmc = clean(aid.text)

            pub_date_node = journal_article.find("Journal/JournalIssue/PubDate") if journal_article is not None else None
            date_text = " ".join(filter(None, [
                first_text(pub_date_node, "Year"),
                first_text(pub_date_node, "Month"),
                first_text(pub_date_node, "Day"),
                first_text(pub_date_node, "MedlineDate")
            ]))
            volume = first_text(journal_article, "Journal/JournalIssue/Volume")
            issue = first_text(journal_article, "Journal/JournalIssue/Issue")
            pages = first_text(journal_article, "Pagination/MedlinePgn")

            records.append({
                "title": title, "authors": authors, "journal": journal,
                "year": year_from(date_text), "date": date_text,
                "volume": volume, "issue": issue, "pages": pages,
                "doi": doi, "pmid": pmid, "pmc": pmc,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                "abstract": " ".join(abstract_parts), "type": "journal-article",
                "sources": ["PubMed"]
            })
    return records


def crossref_by_orcid(orcid: str, email: str, include_types: set[str]) -> list[dict[str, Any]]:
    if not orcid or "REPLACE_" in orcid:
        return []
    cursor = "*"
    records = []
    while cursor:
        params = {
            "filter": f"orcid:{orcid}",
            "rows": 1000,
            "cursor": cursor,
            "cursor-max": 1000,
            "mailto": email
        }
        data = request_json(f"{CROSSREF_BASE}/works", params=params)
        message = data.get("message", {})
        items = message.get("items", [])
        for item in items:
            item_type = clean(item.get("type"))
            if include_types and item_type not in include_types:
                continue
            authors = []
            for a in item.get("author", []):
                name = " ".join(filter(None, [clean(a.get("given")), clean(a.get("family"))]))
                if name:
                    authors.append(name)
            date_parts = (
                item.get("published-print", {}).get("date-parts")
                or item.get("published-online", {}).get("date-parts")
                or item.get("issued", {}).get("date-parts")
                or []
            )
            year = date_parts[0][0] if date_parts and date_parts[0] else None
            doi = normalize_doi(item.get("DOI"))
            records.append({
                "title": clean((item.get("title") or [""])[0]),
                "authors": authors,
                "journal": clean((item.get("container-title") or [""])[0]),
                "year": year, "date": str(year or ""),
                "volume": clean(item.get("volume")), "issue": clean(item.get("issue")),
                "pages": clean(item.get("page")), "doi": doi,
                "pmid": "", "pmc": "",
                "url": f"https://doi.org/{doi}" if doi else clean(item.get("URL")),
                "abstract": clean(item.get("abstract")), "type": item_type,
                "sources": ["Crossref"]
            })
        next_cursor = message.get("next-cursor")
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return records


def get_orcid_token() -> str:
    client_id = os.getenv("ORCID_CLIENT_ID", "").strip()
    client_secret = os.getenv("ORCID_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return ""
    data = request_json(
        ORCID_TOKEN_URL, method="POST",
        headers={"Accept": "application/json"},
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": "/read-public"
        }
    )
    return clean(data.get("access_token"))


def orcid_works(orcid: str, token: str) -> list[dict[str, Any]]:
    if not token or not orcid or "REPLACE_" in orcid:
        return []
    data = request_json(
        f"{ORCID_BASE}/{orcid}/works",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}
    )
    records = []
    for group in data.get("group", []):
        summaries = group.get("work-summary", [])
        if not summaries:
            continue
        summary = summaries[0]
        ext = {}
        for eid in summary.get("external-ids", {}).get("external-id", []):
            ext[clean(eid.get("external-id-type")).lower()] = clean(eid.get("external-id-value"))
        doi = normalize_doi(ext.get("doi"))
        pmid = clean(ext.get("pmid"))
        title = clean(summary.get("title", {}).get("title", {}).get("value"))
        pub_date = summary.get("publication-date") or {}
        year = year_from((pub_date.get("year") or {}).get("value"))
        records.append({
            "title": title, "authors": [], "journal": clean(
                (summary.get("journal-title") or {}).get("value")
            ),
            "year": year, "date": str(year or ""),
            "volume": "", "issue": "", "pages": "",
            "doi": doi, "pmid": pmid, "pmc": "",
            "url": f"https://doi.org/{doi}" if doi else (
                f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
            ),
            "abstract": "", "type": clean(summary.get("type")).lower(),
            "sources": ["ORCID"]
        })
    return records


def merge_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        doi = normalize_doi(record.get("doi"))
        pmid = clean(record.get("pmid"))
        title_key = normalize_title(record.get("title"))
        year = record.get("year") or ""
        key = f"doi:{doi}" if doi else (f"pmid:{pmid}" if pmid else f"title:{title_key}:{year}")

        if key not in merged:
            merged[key] = record
            continue

        current = merged[key]
        for field in ("title", "journal", "date", "volume", "issue", "pages",
                      "doi", "pmid", "pmc", "url", "abstract", "type"):
            if not current.get(field) and record.get(field):
                current[field] = record[field]
        if not current.get("authors") and record.get("authors"):
            current["authors"] = record["authors"]
        current["sources"] = sorted(set(current.get("sources", []) + record.get("sources", [])))
        if not current.get("year") and record.get("year"):
            current["year"] = record["year"]
    return list(merged.values())


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text())
    email = clean(config.get("contact_email"))
    if not email or "YOUR_EMAIL" in email:
        raise RuntimeError("Replace contact_email in config.json with a real address.")

    ncbi_key = os.getenv("NCBI_API_KEY", "").strip()
    include_types = set(config.get("include_types", []))
    all_records: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    token = get_orcid_token()

    for author in config.get("authors", []):
        query = clean(author.get("pubmed_query"))
        orcid = clean(author.get("orcid"))

        if query:
            ids = pubmed_ids(query, email, ncbi_key)
            all_records.extend(fetch_pubmed(ids, email, ncbi_key))

        if orcid and "REPLACE_" not in orcid:
            all_records.extend(crossref_by_orcid(orcid, email, include_types))
            all_records.extend(orcid_works(orcid, token))

    publications = merge_records(all_records)
    earliest = int(config.get("earliest_year", 1900))
    excluded_dois = {normalize_doi(x) for x in config.get("exclude_dois", [])}
    excluded_pmids = {clean(x) for x in config.get("exclude_pmids", [])}

    final = []
    for p in publications:
        if p.get("year") and int(p["year"]) < earliest:
            continue
        if normalize_doi(p.get("doi")) in excluded_dois:
            continue
        if clean(p.get("pmid")) in excluded_pmids:
            continue
        if not p.get("title"):
            review.append({"reason": "missing title", "record": p})
            continue
        p["id"] = hashlib.sha1(
            (normalize_doi(p.get("doi")) or clean(p.get("pmid")) or normalize_title(p["title"])).encode()
        ).hexdigest()[:12]
        p["updated_at"] = datetime.now(timezone.utc).isoformat()
        final.append(p)

    final.sort(key=lambda p: (-(p.get("year") or 0), clean(p.get("title")).lower()))
    OUTPUT_PATH.write_text(json.dumps(final, indent=2, ensure_ascii=False) + "\n")
    REVIEW_PATH.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(final)} publications; {len(review)} records need review.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
