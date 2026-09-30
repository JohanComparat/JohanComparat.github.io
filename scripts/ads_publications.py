#!/usr/bin/env python3
"""Build _data/publications.yml from NASA ADS.

Usage (from the repository root):

    export ADS_API_TOKEN=...   # or store the token in ~/.ads/dev_key
    python3 scripts/ads_publications.py                         # regular update
    python3 scripts/ads_publications.py --bootstrap-from LIST.pdf  # (re)build the section mapping
    python3 scripts/ads_publications.py --check-pdf LIST.pdf       # parse the PDF only, no ADS call

Every ADS record returned by the query is placed in a section using
scripts/pub_sections.yml (section key -> list of bibcodes). Records missing
from the mapping are classified automatically when possible (refereed and
first-authored -> first_author, other refereed articles -> recent) and are
listed at the end of the run so they can be added to the mapping by hand.
Non-refereed records missing from the mapping are left out.

The bootstrap mode reads the local, hand-curated publication list (PDF) and
matches its entries to ADS records by title. Only bibcodes and section keys
are written to the repository, never the PDF itself.
"""

import argparse
import datetime
import difflib
import os
import re
import subprocess
import sys
import unicodedata
from urllib.parse import quote

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTIONS_FILE = os.path.join(ROOT, "scripts", "pub_sections.yml")
OUTPUT_FILE = os.path.join(ROOT, "_data", "publications.yml")

ADS_API = "https://api.adsabs.harvard.edu/v1"
QUERY = 'author:"Comparat, Johan"'
SORT = "date desc, bibcode desc"
ADS_QUERY_URL = ("https://ui.adsabs.harvard.edu/search/q=" + quote(QUERY)
                 + "&sort=" + quote(SORT) + "&p_=0")
FIELDS = ["bibcode", "title", "author", "author_count", "year", "date", "pub",
          "bibstem", "volume", "page", "doi", "identifier", "doctype",
          "property"]
ME = "comparat, j"

# Order and titles of the sections on the publications page.
SECTIONS = [
    ("recent", "Peer-reviewed articles", "Recent articles"),
    ("first_author", "Peer-reviewed articles", "First author"),
    ("phd_students", "Peer-reviewed articles", "Led by PhD students I supervised"),
    ("close", "Peer-reviewed articles", "Close collaboration"),
    ("medium", "Peer-reviewed articles", "Medium-size collaborations"),
    ("consortia", "Peer-reviewed articles", "Large consortia"),
    ("white_papers", "Not peer-reviewed", "White papers"),
    ("arxiv", "Not peer-reviewed", "arXiv preprints"),
    ("proceedings", "Proceedings and abstracts", "Proceedings and abstracts"),
]
SECTION_KEYS = [s[0] for s in SECTIONS] + ["exclude"]

# Section headers of the curated PDF list -> section keys.
PDF_HEADERS = [
    (r"1\.1\s+First author", "first_author"),
    (r"1\.2\s+Supervision of PhD students", "phd_students"),
    (r"1\.3\s+Close collaboration", "close"),
    (r"1\.4\s+Medium size collaborations", "medium"),
    (r"1\.5\s+Large consortia", "consortia"),
    (r"2\.1\s+White papers", "white_papers"),
    (r"2\.2\s+arXiv Publications", "arxiv"),
    (r"3\s+Proceedings", "proceedings"),
]


def norm(text):
    """Lower-case ASCII letters and digits only, for fuzzy comparisons."""
    text = unicodedata.normalize("NFKD", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


# ----------------------------------------------------------------- ADS access

def ads_session():
    token = os.environ.get("ADS_API_TOKEN")
    key_file = os.path.expanduser("~/.ads/dev_key")
    if not token and os.path.exists(key_file):
        token = open(key_file).read().strip()
    if not token:
        sys.exit("Set ADS_API_TOKEN or write the token to ~/.ads/dev_key "
                 "(https://ui.adsabs.harvard.edu/user/settings/token).")
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + token
    return session


def fetch_records(session):
    records, start, rows = [], 0, 200
    while True:
        r = session.get(ADS_API + "/search/query", params={
            "q": QUERY, "fl": ",".join(FIELDS), "sort": SORT,
            "rows": rows, "start": start})
        r.raise_for_status()
        response = r.json()["response"]
        records.extend(response["docs"])
        start += rows
        if start >= response["numFound"]:
            return records


def fetch_metrics(session, bibcodes):
    r = session.post(ADS_API + "/metrics", json={
        "bibcodes": bibcodes, "types": ["indicators", "citations"]})
    if not r.ok:
        print("warning: ADS metrics request failed (%s)" % r.status_code)
        return {}
    data = r.json()
    return {
        "h_index": data.get("indicators", {}).get("h"),
        "citations": data.get("citation stats", {}).get("total number of citations"),
    }


# ------------------------------------------------------ curated PDF (bootstrap)

def parse_pdf(path):
    """Return a list of (section_key, number, text) from the curated PDF list."""
    text = subprocess.run(["pdftotext", "-layout", path, "-"], check=True,
                          capture_output=True, text=True).stdout
    entries, section, expected = [], None, None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or re.fullmatch(r"Page \d+", stripped) \
                or "publication list" in stripped:
            continue
        header = next((key for pattern, key in PDF_HEADERS
                       if re.fullmatch(pattern, stripped)), None)
        if header:
            section = header
            if header == "proceedings":
                expected = 1  # proceedings are numbered separately
            continue
        if section is None:
            continue
        m = re.match(r"(\d+)\.\s+(.*)", stripped)
        if m and (expected is None or int(m.group(1)) == expected):
            number = int(m.group(1))
            entries.append([section, number, m.group(2)])
            expected = number + 1
        elif entries:
            prev = entries[-1][2]
            joiner = "" if prev.endswith("-") else " "
            entries[-1][2] = prev + joiner + stripped
    return [tuple(e) for e in entries]


def pdf_entry_year_title(text):
    """Split 'Authors YEAR[a]. Title. Ref' into (year, title-ish text)."""
    m = re.search(r"\b((?:19|20)\d\d)[a-z]?\b\.?\s+(.*)", text)
    if not m:
        return None, text
    return int(m.group(1)), m.group(2)


def match_pdf_to_ads(entries, records):
    ads = [(rec, norm((rec.get("title") or [""])[0]), int(rec["year"]))
           for rec in records]
    mapping, used, unmatched = {}, set(), []
    for section, number, text in entries:
        year, rest = pdf_entry_year_title(text)
        rest_n = norm(rest)
        best, best_score = None, 0.0
        for rec, title_n, rec_year in ads:
            if rec["bibcode"] in used or not title_n:
                continue
            if year and abs(rec_year - year) > 1:
                continue
            if len(title_n) > 15 and title_n in rest_n:
                score = 1.0
            else:
                sm = difflib.SequenceMatcher(None, title_n, rest_n[:len(title_n)])
                if sm.real_quick_ratio() < 0.9 or sm.quick_ratio() < 0.9:
                    continue
                score = sm.ratio()
            if score > best_score:
                best, best_score = rec, score
        if best is not None and best_score >= 0.9:
            used.add(best["bibcode"])
            mapping.setdefault(section, []).append(best["bibcode"])
        else:
            unmatched.append((section, number, text))
    return mapping, unmatched


# ------------------------------------------------------------ page data output

def short_name(author):
    if "," not in author:
        return author
    last, first = [s.strip() for s in author.split(",", 1)]
    initials = []
    for part in re.split(r"[\s.]+", first):
        if part.startswith("-") and initials:  # "J. -P." -> "J.-P."
            initials[-1] += "-" + part[1:2] + "."
        elif part:
            initials.append("-".join(p[0] + "." for p in part.split("-") if p))
    initials = " ".join(initials)
    return "%s, %s" % (last, initials) if initials else last


def format_authors(authors, count, n=3):
    names = []
    for a in authors[:n]:
        name = short_name(a)
        if norm(a).startswith(norm(ME)):
            name = "<strong>%s</strong>" % name
        names.append(name)
    text = "; ".join(names)
    if (count or len(authors)) > n:
        text += " et al."
    return text


def venue(rec):
    stem = (rec.get("bibstem") or [rec.get("pub", "")])[0]
    if stem in ("arXiv",):
        return "arXiv"
    parts = [stem]
    if rec.get("volume"):
        parts.append(rec["volume"])
    text = " ".join(parts)
    if rec.get("page") and rec["page"][0]:
        text += ", " + rec["page"][0]
    return text


def arxiv_id(rec):
    for ident in rec.get("identifier") or []:
        if ident.startswith("arXiv:"):
            return ident[len("arXiv:"):]
    return None


def entry(rec):
    e = {
        "year": rec["year"],
        "authors": format_authors(rec.get("author") or [], rec.get("author_count")),
        "title": (rec.get("title") or ["(no title)"])[0],
        "venue": venue(rec),
        "ads": "https://ui.adsabs.harvard.edu/abs/%s/abstract" % quote(rec["bibcode"]),
    }
    if rec.get("doi"):
        e["doi"] = "https://doi.org/" + rec["doi"][0]
    arx = arxiv_id(rec)
    if arx:
        e["arxiv"] = "https://arxiv.org/abs/" + arx
    return e


def is_first_author(rec):
    authors = rec.get("author") or []
    return bool(authors) and norm(authors[0]).startswith(norm(ME))


def load_sections():
    if not os.path.exists(SECTIONS_FILE):
        return {}
    data = yaml.safe_load(open(SECTIONS_FILE)) or {}
    bad = set(data) - set(SECTION_KEYS)
    if bad:
        sys.exit("Unknown section keys in %s: %s" % (SECTIONS_FILE, sorted(bad)))
    return {b: key for key, bibcodes in data.items() for b in bibcodes or []}


def save_sections(mapping):
    header = ("# Section of each publication on /publications/, by ADS bibcode.\n"
              "# Keys: %s.\n"
              "# Maintained by scripts/ads_publications.py; edit by hand to reclassify.\n"
              % ", ".join(SECTION_KEYS))
    ordered = {key: sorted(mapping[key], reverse=True)
               for key in SECTION_KEYS if mapping.get(key)}
    with open(SECTIONS_FILE, "w") as f:
        f.write(header)
        yaml.safe_dump(ordered, f, sort_keys=False, allow_unicode=True, width=200)


def build(records, section_of):
    sections = {key: [] for key, _, _ in SECTIONS}
    auto, left_out = [], []
    for rec in records:
        key = section_of.get(rec["bibcode"])
        if key is None:
            refereed = "REFEREED" in (rec.get("property") or [])
            if refereed and rec.get("doctype") == "article":
                key = "first_author" if is_first_author(rec) else "recent"
                auto.append((key, rec))
            else:
                left_out.append(rec)
                continue
        if key == "exclude":
            continue
        sections[key].append(rec)

    out_sections = []
    for key, group, title in SECTIONS:
        recs = sorted(sections[key], key=lambda r: (r.get("date") or "", r["bibcode"]),
                      reverse=True)
        if recs:
            out_sections.append({"key": key, "group": group, "title": title,
                                 "entries": [entry(r) for r in recs]})
    refereed = sum(len(s["entries"]) for s in out_sections
                   if s["group"] == "Peer-reviewed articles")
    data = {
        "generated": datetime.date.today().isoformat(),
        "ads_query_url": ADS_QUERY_URL,
        "total": sum(len(s["entries"]) for s in out_sections),
        "refereed": refereed,
        "sections": out_sections,
    }
    bibcodes = [r["bibcode"] for key in sections for r in sections[key]]
    return data, auto, left_out, bibcodes


def describe(rec):
    return "%s  %s (%s)" % (rec["bibcode"], (rec.get("title") or [""])[0][:90],
                            rec.get("doctype"))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bootstrap-from", metavar="PDF",
                        help="build scripts/pub_sections.yml from the curated PDF list")
    parser.add_argument("--check-pdf", metavar="PDF",
                        help="only parse the curated PDF list and print section counts")
    args = parser.parse_args()

    if args.check_pdf:
        entries = parse_pdf(args.check_pdf)
        for key in SECTION_KEYS:
            n = sum(1 for e in entries if e[0] == key)
            if n:
                print("%-14s %4d" % (key, n))
        print("%-14s %4d" % ("total", len(entries)))
        return

    session = ads_session()
    records = fetch_records(session)
    print("ADS returned %d records for %s" % (len(records), QUERY))

    if args.bootstrap_from:
        mapping, unmatched = match_pdf_to_ads(parse_pdf(args.bootstrap_from), records)
        save_sections(mapping)
        print("Wrote %s:" % os.path.relpath(SECTIONS_FILE, ROOT))
        for key in SECTION_KEYS:
            if mapping.get(key):
                print("  %-14s %4d" % (key, len(mapping[key])))
        if unmatched:
            print("\n%d PDF entries not matched to an ADS record "
                  "(add their bibcodes to the mapping by hand):" % len(unmatched))
            for section, number, text in unmatched:
                print("  [%s #%d] %s" % (section, number, text[:140]))

    data, auto, left_out, bibcodes = build(records, load_sections())
    metrics = fetch_metrics(session, bibcodes) if bibcodes else {}
    data["metrics"] = metrics

    with open(OUTPUT_FILE, "w") as f:
        f.write("# Generated by scripts/ads_publications.py; do not edit by hand.\n")
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=1000)
    print("\nWrote %s: %d publications (%d refereed), metrics %s"
          % (os.path.relpath(OUTPUT_FILE, ROOT), data["total"], data["refereed"], metrics))
    for s in data["sections"]:
        print("  %-14s %4d" % (s["key"], len(s["entries"])))

    if auto:
        print("\n%d refereed articles not in the mapping, classified automatically:" % len(auto))
        for key, rec in auto:
            print("  -> %-12s %s" % (key, describe(rec)))
    if left_out:
        print("\n%d non-refereed records not in the mapping, left out:" % len(left_out))
        for rec in left_out:
            print("  %s" % describe(rec))


if __name__ == "__main__":
    main()
