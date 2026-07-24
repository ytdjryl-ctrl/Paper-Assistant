from __future__ import annotations

import html
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from urllib.parse import quote

import requests


DEFAULT_SOURCES = ("crossref", "openalex", "sciencedirect")
_OPENALEX_COOLDOWN_LOCK = Lock()
_OPENALEX_DISABLED_UNTIL = 0.0


class OpenAlexRateLimited(RuntimeError):
    """Raised while OpenAlex is cooling down after an HTTP 429 response."""


def _clean(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first(value: Any) -> str:
    if isinstance(value, list):
        return _clean(value[0]) if value else ""
    return _clean(value)


def _year_from_parts(parts: Any) -> str:
    try:
        return str(parts[0][0])
    except (IndexError, KeyError, TypeError):
        return ""


def _abstract_from_inverted_index(index: Any) -> str:
    if not isinstance(index, dict):
        return ""
    positioned: List[Tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        positioned.extend((int(position), str(word)) for position in positions)
    return " ".join(word for _, word in sorted(positioned))


def _authors_from_crossref(authors: Any) -> str:
    names = []
    for author in authors if isinstance(authors, list) else []:
        if not isinstance(author, dict):
            continue
        name = " ".join(part for part in (_clean(author.get("given")), _clean(author.get("family"))) if part)
        if name:
            names.append(name)
    return ", ".join(names)


def _authors_from_openalex(authorships: Any) -> str:
    names = []
    for authorship in authorships if isinstance(authorships, list) else []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        name = _clean(author.get("display_name")) if isinstance(author, dict) else ""
        if name:
            names.append(name)
    return ", ".join(names)


def _session() -> requests.Session:
    session = requests.Session()
    contact = os.getenv("ACADEMIC_SEARCH_MAILTO", "").strip()
    agent = "SciAssistantV2/2.0"
    if contact:
        agent += f" (mailto:{contact})"
    session.headers.update({"User-Agent": agent, "Accept": "application/json"})
    return session


def search_crossref(query: str, limit: int, session: requests.Session) -> List[Dict[str, Any]]:
    params = {
        "query.bibliographic": query,
        "rows": limit,
        "select": "DOI,title,author,published,container-title,URL,abstract,type,publisher",
    }
    mailto = os.getenv("CROSSREF_MAILTO", os.getenv("ACADEMIC_SEARCH_MAILTO", "")).strip()
    if mailto:
        params["mailto"] = mailto
    response = session.get("https://api.crossref.org/works", params=params, timeout=30)
    response.raise_for_status()
    items = response.json().get("message", {}).get("items", [])
    output = []
    for item in items:
        if not isinstance(item, dict):
            continue
        doi = _clean(item.get("DOI"))
        output.append({
            "title": _first(item.get("title")),
            "authors": _authors_from_crossref(item.get("author")),
            "year": _year_from_parts((item.get("published") or {}).get("date-parts")),
            "journal": _first(item.get("container-title")),
            "doi": doi,
            "url": _clean(item.get("URL")) or (f"https://doi.org/{doi}" if doi else ""),
            "abstract": _clean(item.get("abstract")),
            "publisher": _clean(item.get("publisher")),
            "work_type": _clean(item.get("type")),
            "source": "crossref",
        })
    return [item for item in output if item["title"] and item["url"]]


def search_openalex(query: str, limit: int, session: requests.Session) -> List[Dict[str, Any]]:
    global _OPENALEX_DISABLED_UNTIL
    with _OPENALEX_COOLDOWN_LOCK:
        remaining = _OPENALEX_DISABLED_UNTIL - time.monotonic()
    if remaining > 0:
        raise OpenAlexRateLimited(f"OpenAlex cooldown active ({remaining:.0f}s remaining)")
    params: Dict[str, Any] = {"search": query, "per-page": limit}
    mailto = os.getenv("OPENALEX_MAILTO", os.getenv("ACADEMIC_SEARCH_MAILTO", "")).strip()
    if mailto:
        params["mailto"] = mailto
    response = session.get("https://api.openalex.org/works", params=params, timeout=30)
    if response.status_code == 429:
        try:
            retry_after = max(30.0, min(float(response.headers.get("Retry-After", "120")), 900.0))
        except (TypeError, ValueError):
            retry_after = 120.0
        with _OPENALEX_COOLDOWN_LOCK:
            _OPENALEX_DISABLED_UNTIL = max(_OPENALEX_DISABLED_UNTIL, time.monotonic() + retry_after)
        raise OpenAlexRateLimited(f"OpenAlex returned HTTP 429; cooling down for {retry_after:.0f}s")
    response.raise_for_status()
    output = []
    for item in response.json().get("results", []):
        if not isinstance(item, dict):
            continue
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {} if isinstance(primary, dict) else {}
        doi_url = _clean(item.get("doi"))
        doi = re.sub(r"^https?://doi\.org/", "", doi_url, flags=re.IGNORECASE)
        output.append({
            "title": _clean(item.get("display_name") or item.get("title")),
            "authors": _authors_from_openalex(item.get("authorships")),
            "year": _clean(item.get("publication_year")),
            "journal": _clean(source.get("display_name")) if isinstance(source, dict) else "",
            "doi": doi,
            "url": doi_url or _clean(primary.get("landing_page_url")) or _clean(item.get("id")),
            "abstract": _abstract_from_inverted_index(item.get("abstract_inverted_index")),
            "cited_by_count": item.get("cited_by_count"),
            "open_access": (item.get("open_access") or {}).get("is_oa") if isinstance(item.get("open_access"), dict) else None,
            "source": "openalex",
        })
    return [item for item in output if item["title"] and item["url"]]


def search_sciencedirect(query: str, limit: int, session: requests.Session) -> List[Dict[str, Any]]:
    api_key = os.getenv("ELSEVIER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ELSEVIER_API_KEY is not configured")
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
    inst_token = os.getenv("ELSEVIER_INST_TOKEN", "").strip()
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    response = session.get(
        "https://api.elsevier.com/content/search/sciencedirect",
        params={"query": query, "count": min(limit, 100), "httpAccept": "application/json"},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    entries = response.json().get("search-results", {}).get("entry", [])
    output = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        links = item.get("link") or []
        url = ""
        for link in links if isinstance(links, list) else []:
            if isinstance(link, dict) and link.get("@href"):
                url = _clean(link["@href"])
                if link.get("@ref") in {"scidir", "self"}:
                    break
        doi = _clean(item.get("prism:doi"))
        output.append({
            "title": _clean(item.get("dc:title")),
            "authors": _clean(item.get("dc:creator")),
            "year": _clean(item.get("prism:coverDate"))[:4],
            "journal": _clean(item.get("prism:publicationName")),
            "doi": doi,
            "url": url or (f"https://doi.org/{doi}" if doi else ""),
            "abstract": _clean(item.get("dc:description")),
            "source": "sciencedirect",
        })
    return [item for item in output if item["title"] and item["url"]]


def _nested_text(value: Any) -> str:
    """Flatten Elsevier's variable JSON full-text representation."""
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, list):
        return "\n".join(part for item in value if (part := _nested_text(item)))
    if isinstance(value, dict):
        preferred = []
        for key, item in value.items():
            if key.startswith("@") or key in {"link", "attachment"}:
                continue
            part = _nested_text(item)
            if part:
                preferred.append(part)
        return "\n".join(preferred)
    return ""


def fetch_sciencedirect_article(
    identifier: str,
    *,
    id_type: str = "doi",
    session: requests.Session | None = None,
) -> Dict[str, Any]:
    """Retrieve an Elsevier article through the official Article Retrieval API."""
    api_key = os.getenv("ELSEVIER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ELSEVIER_API_KEY is not configured")
    normalized_type = id_type.strip().lower()
    if normalized_type not in {"doi", "pii", "scopus_id"}:
        raise ValueError("id_type must be doi, pii, or scopus_id")
    identifier = str(identifier or "").strip()
    if not identifier:
        raise ValueError("identifier is required")

    own_session = session is None
    active_session = session or _session()
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
    inst_token = os.getenv("ELSEVIER_INST_TOKEN", "").strip()
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    endpoint_type = "scopus_id" if normalized_type == "scopus_id" else normalized_type
    try:
        response = active_session.get(
            f"https://api.elsevier.com/content/article/{endpoint_type}/{quote(identifier, safe='')}",
            params={"view": "FULL", "httpAccept": "application/json"},
            headers=headers,
            timeout=(10, 60),
        )
        response.raise_for_status()
        payload = response.json().get("full-text-retrieval-response", {})
        core = payload.get("coredata") or {}
        original = payload.get("originalText") or payload.get("body") or ""
        content = _nested_text(original)
        abstract = _clean(core.get("dc:description"))
        doi = _clean(core.get("prism:doi"))
        title = _clean(core.get("dc:title"))
        result = {
            "identifier": identifier,
            "id_type": normalized_type,
            "title": title,
            "doi": doi,
            "journal": _clean(core.get("prism:publicationName")),
            "publication_date": _clean(core.get("prism:coverDate")),
            "abstract": abstract,
            "content": content or abstract,
            "full_text_available": bool(content),
            "access_status": "full_text" if content else "metadata_abstract_only",
            "source": "elsevier_api",
            "source_url": _clean(core.get("prism:url")) or (f"https://doi.org/{doi}" if doi else ""),
        }
        if not result["content"] and not result["title"]:
            raise RuntimeError("Elsevier returned no accessible metadata or content")
        return result
    finally:
        if own_session:
            active_session.close()


PROVIDERS = {
    "crossref": search_crossref,
    "openalex": search_openalex,
    "sciencedirect": search_sciencedirect,
}


def configured_sources(value: str | None = None) -> List[str]:
    raw = value if value is not None else os.getenv("ACADEMIC_SEARCH_SOURCES", ",".join(DEFAULT_SOURCES))
    sources = []
    for source in raw.split(","):
        normalized = source.strip().lower()
        if normalized == "sciencedirect" and not os.getenv("ELSEVIER_API_KEY", "").strip():
            continue
        if normalized and normalized in PROVIDERS and normalized not in sources:
            sources.append(normalized)
    return sources


def search_academic_sources(
    queries: Sequence[str],
    *,
    sources: Sequence[str] | None = None,
    max_results_per_query: int = 5,
    max_workers: int = 6,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    # Apply optional-provider filtering even when a caller explicitly supplies
    # the source list. ScienceDirect must never turn an otherwise valid search
    # into a configuration failure for users who only set the primary LLM API.
    selected = configured_sources(",".join(sources)) if sources is not None else configured_sources()
    blocks: List[Dict[str, Any]] = []
    warnings: List[str] = []
    if not selected:
        requested = [source.strip().lower() for source in (sources or []) if source.strip()]
        if requested and set(requested) == {"sciencedirect"} and not os.getenv("ELSEVIER_API_KEY", "").strip():
            return [], []
        return [], ["No supported academic search source is enabled."]
    work = [(query, source) for query in queries if query for source in selected if source != "openalex"]
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(work)))) as executor:
        futures = {}
        for query, source in work:
            session = _session()
            future = executor.submit(PROVIDERS[source], query, max_results_per_query, session)
            futures[future] = (query, source, session)
        for future in as_completed(futures):
            query, source, session = futures[future]
            try:
                results = future.result()
                blocks.append({"query": query, "source": source, "success": True, "results": results})
            except Exception as exc:
                message = f"{source} search failed for '{query}': {exc}"
                warnings.append(message)
                blocks.append({"query": query, "source": source, "success": False, "error": str(exc), "results": []})
            finally:
                session.close()
    # OpenAlex applies a comparatively strict rate limit. Run its seed queries
    # sequentially and stop the batch on the first 429/cooldown signal instead
    # of launching a burst that can only generate identical failures.
    if "openalex" in selected:
        openalex_queries = [query for query in queries if query]
        for index, query in enumerate(openalex_queries):
            session = _session()
            try:
                results = search_openalex(query, max_results_per_query, session)
                blocks.append({"query": query, "source": "openalex", "success": True, "results": results})
            except OpenAlexRateLimited as exc:
                warnings.append(f"openalex rate limited; skipped the remaining {len(openalex_queries) - index} query/queries: {exc}")
                blocks.append({"query": query, "source": "openalex", "success": False, "error": str(exc), "results": []})
                for skipped_query in openalex_queries[index + 1:]:
                    blocks.append({
                        "query": skipped_query,
                        "source": "openalex",
                        "success": False,
                        "error": "skipped after OpenAlex rate limit",
                        "results": [],
                    })
                break
            except Exception as exc:
                message = f"openalex search failed for '{query}': {exc}"
                warnings.append(message)
                blocks.append({"query": query, "source": "openalex", "success": False, "error": str(exc), "results": []})
            finally:
                session.close()
    source_order = {source: index for index, source in enumerate(selected)}
    query_order = {query: index for index, query in enumerate(queries)}
    blocks.sort(key=lambda block: (query_order.get(block["query"], 999), source_order.get(block["source"], 999)))
    return blocks, warnings
