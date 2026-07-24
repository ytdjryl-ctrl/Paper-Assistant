import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.tools.academic_search import fetch_sciencedirect_article
from src.tools.mcp_client import INFORMATION_SEEKER_TOOLS
from src.tools.mcp_tools import MCPTools
from src.tools.pubmed_client import PubMedClient, parse_pubmed_xml


PUBMED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345</PMID>
      <Article>
        <Journal><JournalIssue><PubDate><Year>2024</Year><Month>Jun</Month></PubDate></JournalIssue><Title>Journal A</Title></Journal>
        <ArticleTitle>A <i>useful</i> paper</ArticleTitle>
        <Abstract><AbstractText Label="BACKGROUND">First part.</AbstractText><AbstractText>Second part.</AbstractText></Abstract>
        <AuthorList><Author><ForeName>Ada</ForeName><LastName>Lovelace</LastName></Author></AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/example</ArticleId><ArticleId IdType="pmc">PMC999</ArticleId></ArticleIdList></PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


class _Response:
    def __init__(self, *, content=b"", payload=None, status_code=200):
        self.content = content
        self._payload = payload or {}
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _ElsevierSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(payload=self.payload)


class LiteratureRetrievalTests(unittest.TestCase):
    def test_pubmed_xml_parser_preserves_structured_abstract_and_ids(self):
        records = parse_pubmed_xml(PUBMED_XML)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pmid"], "12345")
        self.assertEqual(records[0]["title"], "A useful paper")
        self.assertIn("BACKGROUND: First part.", records[0]["abstract"])
        self.assertEqual(records[0]["doi"], "10.1/example")
        self.assertEqual(records[0]["pmc_id"], "PMC999")

    def test_pubmed_metadata_uses_one_batch_and_then_disk_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = PubMedClient(cache_dir=Path(temp_dir))
            calls = []

            def fake_get(endpoint, params):
                calls.append((endpoint, params))
                return _Response(content=PUBMED_XML)

            client._get = fake_get
            first = client.fetch_metadata(["12345"])
            second = client.fetch_metadata(["12345"])
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1]["id"], "12345")
            client.close()

    def test_elsevier_retrieval_uses_key_header_without_exposing_it_in_result(self):
        payload = {
            "full-text-retrieval-response": {
                "coredata": {
                    "dc:title": "Elsevier paper",
                    "dc:description": "Abstract",
                    "prism:doi": "10.2/elsevier",
                },
                "originalText": {"body": {"section": ["Methods", "Results"]}},
            }
        }
        session = _ElsevierSession(payload)
        with patch.dict(os.environ, {"ELSEVIER_API_KEY": "secret-test-key"}, clear=False):
            result = fetch_sciencedirect_article("10.2/elsevier", session=session)
        self.assertTrue(result["full_text_available"])
        self.assertIn("Methods", result["content"])
        self.assertNotIn("secret-test-key", str(result))
        self.assertEqual(session.calls[0][1]["headers"]["X-ELS-APIKey"], "secret-test-key")

    def test_missing_full_text_is_successful_abstract_fallback(self):
        class FakeClient:
            def fetch_metadata(self, pmids):
                return [{
                    "pmid": pmids[0], "title": "Paper", "abstract": "Verified abstract",
                    "pmc_id": "", "doi": "", "url": "https://pubmed.ncbi.nlm.nih.gov/12345/",
                }]

        tools = object.__new__(MCPTools)
        tools._get_pubmed_client = lambda: FakeClient()
        with patch.dict(os.environ, {"ELSEVIER_API_KEY": ""}, clear=False):
            result = tools.get_pubmed_article("12345")
        self.assertTrue(result.success)
        self.assertEqual(result.data["access_status"], "metadata_abstract_only")
        self.assertEqual(result.data["content"], "Verified abstract")
        self.assertTrue(result.metadata["do_not_retry_full_text"])

    def test_information_seeker_can_use_structured_and_elsevier_tools(self):
        self.assertIn("academic_search", INFORMATION_SEEKER_TOOLS)
        self.assertIn("get_sciencedirect_article", INFORMATION_SEEKER_TOOLS)


if __name__ == "__main__":
    unittest.main()
