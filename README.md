# Holm Lab Publications Pipeline

## Overview

This repository automatically builds and publishes the **Holm Lab Publications** database for the lab website.

The purpose of this project is to maintain a single authoritative list of publications without manually editing the Squarespace website whenever a new paper is published.

The workflow automatically retrieves publications from multiple scholarly databases, merges duplicate records, filters publications to include only those associated with Johanna B. Holm's institutional appointments, generates a clean JSON file, and publishes that file through GitHub Pages. The Squarespace website then reads the JSON file and displays the publication list automatically.

---

# Overall Architecture

```
GitHub Actions
        │
        ▼
update_publications.py
        │
        ▼
PubMed
Crossref
OpenAlex
(Optional ORCID)
        │
        ▼
Merge & Deduplicate
        │
        ▼
Institution Filter
        │
        ▼
publications.json
        │
        ▼
GitHub Pages
        │
        ▼
Squarespace Publication Page
```

The website never stores publication data itself.

Instead, Squarespace simply downloads the latest version of `publications.json` whenever someone visits the Publications page.

---

# Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── update-publications.yml
│
├── update_publications.py
├── config.json
├── publications.json
├── publication-review.json
└── README.md
```

---

# Important Files

## update_publications.py

This is the main application.

Responsibilities include:

* Query PubMed
* Query Crossref
* Query OpenAlex
* Query ORCID (optional)
* Normalize publication metadata
* Merge duplicate publications
* Filter publications by affiliation
* Generate publications.json
* Generate publication-review.json

This is the only file that should require regular programming changes.

---

## config.json

Contains configuration values only.

Typical settings include

* contact email
* ORCID
* PubMed search query
* publication types
* earliest publication year
* allowed institutional affiliations

No publication data is stored here.

---

## publications.json

Automatically generated.

Never edit manually.

This file is read directly by the Squarespace website.

---

## publication-review.json

Automatically generated.

Contains publications that were excluded or require manual review.

Examples include

* missing title
* missing publication year
* excluded affiliation
* failed metadata lookup

This file is primarily for debugging.

---

# Publication Sources

The script gathers publications from several independent sources.

## 1. PubMed

Primary source.

Advantages

* High-quality metadata
* PMIDs
* Author affiliations
* Reliable publication dates

---

## 2. Crossref

Used to identify publications that may not appear in PubMed.

Advantages

* DOI metadata
* Citation information
* Journal metadata

---

## 3. OpenAlex

Used primarily to recover publications that are missing from PubMed or Crossref.

Advantages

* Excellent historical coverage
* Institutional affiliations
* Open-access links
* Additional metadata

---

## 4. ORCID (optional)

ORCID is queried when API credentials are available.

ORCID is mainly used as another source for identifying publications.

Its affiliation information is generally not sufficient for filtering publications by institution.

If ORCID credentials are unavailable, the script continues normally.

---

# Why Multiple Sources?

No single database contains every publication.

Using multiple sources greatly improves completeness.

Typical strategy

PubMed
↓

Crossref

↓

OpenAlex

↓

ORCID

↓

Merge everything

---

# Duplicate Detection

Many publications appear in multiple databases.

Duplicates are merged automatically.

Matching priority

1. DOI

2. PMID

3. Normalized title + publication year

The merged record keeps the best metadata from every source.

---

# Institutional Filtering

Not every publication returned by scholarly databases belongs on the lab website.

After merging, publications are filtered using Johanna Holm's institutional affiliations.

Currently allowed institutions

* Millersville University
* Cornell University
* University of Southern California
* University of Maryland School of Medicine

Common institutional aliases are also recognized.

Examples

* Weill Cornell
* Keck School of Medicine
* UMB School of Medicine
* University of Maryland Baltimore

Only publications where **Johanna Holm herself** has one of these affiliations are retained.

This prevents publications from unrelated authors with similar names from appearing.

---

# Abstracts

Abstracts are intentionally removed.

Reasons include

* faster page loading
* smaller JSON file
* cleaner website
* reduced bandwidth
* publications page is intended to be a bibliography, not a literature database

---

# GitHub Actions

GitHub Actions automatically executes the publication pipeline.

Typical workflow

```
GitHub Action
↓

Run update_publications.py

↓

Generate publications.json

↓

Commit updated files

↓

Deploy GitHub Pages
```

No manual intervention is normally required.

---

# GitHub Pages

GitHub Pages hosts

```
publications.json
```

The public URL is consumed by the Squarespace website.

The publication page should never contain hard-coded publication data.

---

# Squarespace Website

Squarespace only displays the data.

It does not store publication information.

The publication page

* downloads publications.json
* groups publications by year
* provides search
* provides filters
* displays DOI/PubMed links

If the publication list is incorrect, the problem is almost always in the GitHub repository rather than Squarespace.

---

# Updating Publications

Normally no action is required.

Whenever the GitHub Action runs

1. query publication databases

2. merge records

3. filter affiliations

4. create publications.json

5. deploy GitHub Pages

The website updates automatically.

---

# Manual Update

To run locally

```
python update_publications.py
```

or

```
python -m py_compile update_publications.py
python update_publications.py
```

---

# Troubleshooting

## Crossref 400 Error

Usually caused by unsupported parameters.

Verify that

```
cursor=*
```

is used.

Do **not** use

```
cursor-max
```

---

## ORCID Credentials Missing

Message

```
ORCID credentials are missing.
```

This is not fatal.

The script continues using

* PubMed
* Crossref
* OpenAlex

---

## Missing Publications

Possible causes

* affiliation not recognized
* publication missing from databases
* excluded by configuration
* duplicate merged incorrectly

First inspect

```
publication-review.json
```

---

## Website Does Not Update

Check

1. GitHub Action completed successfully.

2. publications.json exists.

3. GitHub Pages deployed successfully.

4. The JSON URL loads in a browser.

5. Squarespace points to the correct JSON URL.

---

# Updating Institutional Affiliations

Allowed affiliations are defined in

```
config.json
```

If Johanna joins another institution in the future

1. Add the new institution.

2. Add common aliases in

```
AFFILIATION_ALIASES
```

inside

```
update_publications.py
```

---

# Security

Never commit

* ORCID secrets
* API tokens
* GitHub Personal Access Tokens

These should always be stored as GitHub Secrets.

---

# Future Improvements

Possible enhancements include

* Altmetric badges
* Citation counts
* Publication metrics
* Topic tags
* Search by author
* PDF availability
* Featured publications
* BibTeX download
* APA/MLA citation export

---

# Repository Ownership

This repository was originally developed for the **Johanna B. Holm Lab** to automate publication management and website integration.

The design goal is to ensure that publications can be maintained with minimal manual effort while keeping the website synchronized with authoritative scholarly databases.

If the repository needs maintenance in the future, the recommended approach is to preserve the overall architecture—automated data retrieval, metadata normalization, affiliation-based filtering, JSON generation, and GitHub Pages deployment—rather than manually editing publication lists on the website.

