from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

_RATE_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _xml_text(node: Optional[ET.Element]) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _article_date(article: ET.Element) -> str:
    pub_date = article.find(".//JournalIssue/PubDate")
    if pub_date is None:
        return ""
    year = _xml_text(pub_date.find("Year"))
    month = _xml_text(pub_date.find("Month"))
    day = _xml_text(pub_date.find("Day"))
    if year:
        return "-".join(part for part in (year, month, day) if part)
    return _xml_text(pub_date.find("MedlineDate"))


def parse_pubmed_xml(content: bytes | str) -> List[Dict[str, Any]]:
    """Parse a PubMed EFetch response into stable, citation-ready records."""
    root = ET.fromstring(content)
    records: List[Dict[str, Any]] = []
    for item in root.findall(".//PubmedArticle"):
        citation = item.find("MedlineCitation")
        article = item.find(".//Article")
        if citation is None or article is None:
            continue
        pmid = _xml_text(citation.find("PMID"))
        title = _xml_text(article.find("ArticleTitle"))
        abstract_parts = []
        for node in article.findall(".//Abstract/AbstractText"):
            value = _xml_text(node)
            label = (node.attrib.get("Label") or "").strip()
            if value:
                abstract_parts.append(f"{label}: {value}" if label else value)

        authors = []
        for author in article.findall(".//AuthorList/Author"):
            collective = _xml_text(author.find("CollectiveName"))
            if collective:
                authors.append(collective)
                continue
            name = " ".join(
                part for part in (
                    _xml_text(author.find("ForeName")),
                    _xml_text(author.find("LastName")),
                ) if part
            )
            if name:
                authors.append(name)

        identifiers = {}
        for identifier in item.findall(".//PubmedData/ArticleIdList/ArticleId"):
            id_type = (identifier.attrib.get("IdType") or "").lower()
            value = _xml_text(identifier)
            if id_type and value:
                identifiers[id_type] = value
        doi = identifiers.get("doi", "")
        pmc_id = identifiers.get("pmc", "")
        records.append({
            "pmid": pmid,
            "title": title,
            "authors": authors,
            "authors_text": ", ".join(authors),
            "journal": _xml_text(article.find(".//Journal/Title")),
            "publication_date": _article_date(article),
            "abstract": "\n".join(abstract_parts),
            "doi": doi,
            "pmc_id": pmc_id,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
            "full_text_available": bool(pmc_id),
            "source": "pubmed",
        })
    return records


class PubMedClient:
    """Small E-utilities client with batching, retries, rate limiting and disk cache."""

    def __init__(self, cache_dir: str | Path | None = None):
        self.api_key = os.getenv("NCBI_API_KEY", "").strip()
        self.email = os.getenv("NCBI_EMAIL", os.getenv("ACADEMIC_SEARCH_MAILTO", "")).strip()
        self.tool = os.getenv("NCBI_TOOL", "SciAssistantV2").strip() or "SciAssistantV2"
        default_rps = 8.0 if self.api_key else 2.5
        self.requests_per_second = max(0.2, _env_float("PUBMED_REQUESTS_PER_SECOND", default_rps))
        self.cache_ttl_seconds = max(0.0, _env_float("PUBMED_CACHE_TTL_DAYS", 30.0) * 86400)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self._failure_lock = threading.Lock()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        user_agent = f"{self.tool}/2.0"
        if self.email:
            user_agent += f" ({self.email})"
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/xml"})

    def _params(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(extra)
        params["tool"] = self.tool
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _wait_for_slot(self) -> None:
        global _LAST_REQUEST_AT
        with _RATE_LOCK:
            interval = 1.0 / self.requests_per_second
            remaining = interval - (time.monotonic() - _LAST_REQUEST_AT)
            if remaining > 0:
                time.sleep(remaining)
            _LAST_REQUEST_AT = time.monotonic()

    def _get(self, endpoint: str, params: Dict[str, Any]) -> requests.Response:
        with self._failure_lock:
            if time.monotonic() < self._circuit_open_until:
                remaining = int(self._circuit_open_until - time.monotonic()) + 1
                raise RuntimeError(
                    f"PubMed circuit breaker is active for {remaining}s after repeated network failures"
                )
        retries = max(1, int(_env_float("PUBMED_MAX_RETRIES", 3)))
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            self._wait_for_slot()
            try:
                response = self.session.get(
                    f"{EUTILS_BASE}/{endpoint}",
                    params=self._params(params),
                    timeout=(10, 45),
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"NCBI temporary response {response.status_code}", response=response
                    )
                response.raise_for_status()
                with self._failure_lock:
                    self._consecutive_failures = 0
                    self._circuit_open_until = 0.0
                return response
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    retry_after = 0.0
                    if getattr(exc, "response", None) is not None:
                        try:
                            retry_after = float(exc.response.headers.get("Retry-After", 0))
                        except (TypeError, ValueError):
                            retry_after = 0.0
                    time.sleep(max(retry_after, min(2 ** attempt, 8)))
        with self._failure_lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                cooldown = max(10.0, _env_float("PUBMED_CIRCUIT_BREAKER_SECONDS", 120.0))
                self._circuit_open_until = time.monotonic() + cooldown
                logger.warning(
                    "PubMed circuit breaker opened for %.0fs after repeated failures", cooldown
                )
        raise RuntimeError(f"NCBI request failed after {retries} attempts: {last_error}")

    def _cache_path(self, namespace: str, key: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{namespace}_{digest}.json"

    def _cache_read(self, namespace: str, key: str) -> Any:
        path = self._cache_path(namespace, key)
        if not path or not path.exists():
            return None
        try:
            if self.cache_ttl_seconds and time.time() - path.stat().st_mtime > self.cache_ttl_seconds:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _cache_write(self, namespace: str, key: str, value: Any) -> None:
        path = self._cache_path(namespace, key)
        if not path:
            return
        try:
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.debug("PubMed cache write skipped: %s", exc)

    def search(self, query: str, max_results: int = 10) -> List[str]:
        max_results = max(1, min(int(max_results), 100))
        cache_key = f"{query}|{max_results}"
        cached = self._cache_read("search", cache_key)
        if isinstance(cached, list):
            return [str(value) for value in cached]
        response = self._get("esearch.fcgi", {
            "db": "pubmed", "term": query, "retmax": max_results, "retmode": "xml"
        })
        root = ET.fromstring(response.content)
        pmids = [_xml_text(node) for node in root.findall(".//IdList/Id") if _xml_text(node)]
        self._cache_write("search", cache_key, pmids)
        return pmids

    def fetch_metadata(self, pmids: Iterable[str]) -> List[Dict[str, Any]]:
        ordered = list(dict.fromkeys(str(pmid).strip() for pmid in pmids if str(pmid).strip()))
        if not ordered:
            return []
        by_pmid: Dict[str, Dict[str, Any]] = {}
        missing = []
        for pmid in ordered:
            cached = self._cache_read("record", pmid)
            if isinstance(cached, dict):
                by_pmid[pmid] = cached
            else:
                missing.append(pmid)
        for start in range(0, len(missing), 100):
            batch = missing[start:start + 100]
            response = self._get("efetch.fcgi", {
                "db": "pubmed", "id": ",".join(batch), "retmode": "xml"
            })
            for record in parse_pubmed_xml(response.content):
                pmid = record.get("pmid", "")
                if pmid:
                    by_pmid[pmid] = record
                    self._cache_write("record", pmid, record)
        return [by_pmid[pmid] for pmid in ordered if pmid in by_pmid]

    def fetch_open_full_text(self, pmc_id: str) -> Optional[Dict[str, Any]]:
        pmc_id = str(pmc_id or "").strip().upper()
        if not pmc_id:
            return None
        cached = self._cache_read("fulltext", pmc_id)
        if isinstance(cached, dict):
            return cached
        try:
            response = self.session.get(
                f"{EUROPE_PMC_BASE}/{pmc_id}/fullTextXML",
                timeout=(10, 60),
                headers={"Accept": "application/xml"},
            )
            if response.status_code == 200 and response.content:
                root = ET.fromstring(response.content)
                body = root.find(".//body")
                content = _xml_text(body)
                if content:
                    result = {
                        "content": content,
                        "source": "europe_pmc",
                        "source_url": f"https://europepmc.org/articles/{pmc_id}",
                    }
                    self._cache_write("fulltext", pmc_id, result)
                    return result
        except (requests.RequestException, ET.ParseError) as exc:
            logger.info("Europe PMC full text unavailable for %s: %s", pmc_id, exc)

        try:
            url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc_id}/"
            response = self.session.get(url, timeout=(10, 60), headers={"Accept": "text/html"})
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                main = soup.select_one("main") or soup.select_one("article")
                content = "\n".join(main.stripped_strings) if main else ""
                if content:
                    result = {"content": content, "source": "pmc", "source_url": url}
                    self._cache_write("fulltext", pmc_id, result)
                    return result
        except requests.RequestException as exc:
            logger.info("PMC HTML unavailable for %s: %s", pmc_id, exc)
        return None

    def close(self) -> None:
        self.session.close()
