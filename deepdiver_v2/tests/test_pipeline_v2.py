from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Unit tests must never call the configured external figure-planning model.
os.environ["FIGURE_AGENT_USE_PRIMARY_MODEL"] = "false"
os.environ["VISUAL_PLANNER_USE_PRIMARY_MODEL"] = "false"


DEEPDIVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = DEEPDIVER_ROOT.parent
sys.path.insert(0, str(DEEPDIVER_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline_v2.extraction import extract_source_files
from src.pipeline_v2.experiment_agent import _read_results
from src.pipeline_v2.file_inventory import collect_source_files
from src.pipeline_v2.figure_planner import build_figure_plan
from src.pipeline_v2.figure_critic import audit_figure
from src.pipeline_v2.hybrid import build_autonomous_agent_brief, review_revision_loop, seed_hybrid_literature
from src.pipeline_v2.literature import (
    assign_reference_indices,
    deduplicate_references,
    load_existing_references,
    parse_reference_records_from_markdown,
)
from src.pipeline_v2.models import LiteratureTask, PipelineContext, ReferenceRecord, SourceFile
from src.pipeline_v2.reference_export import write_reference_download_txt
from src.pipeline_v2.research_contract import build_research_contract
from src.pipeline_v2.retrieval import retrieve_online_references
from src.pipeline_v2.review import (
    REVIEW_ORDER,
    _reviewer_max_tokens,
    _reviewer_temperature,
    _review_one,
    audit_citations,
    get_reviewer_model_config,
    run_reviews,
    synchronize_citations,
)
from src.pipeline_v2.runner import PipelineV2
from src.pipeline_v2.writer import compact_citations
from src.pipeline_v2.visual_communication import audit_manuscript_visuals, plan_visual_communication
from src.agents.base_agent import AgentConfig, BaseAgent
from src.agents.planner_agent import PlannerAgent
from src.tools.academic_search import configured_sources, search_academic_sources
from src.tools.mcp_tools import get_tool_schemas
from src.utils.task_manager import TaskManager, TaskStatus, get_task_manager
from src.utils.llm_client import stream_chat_completion_response
from config.logging_config import MojibakeGuardFilter


class FakeResult:
    def __init__(self, success=True, data=None, error=None):
        self.success = success
        self.data = data
        self.error = error


class FakeSearchTools:
    def batch_web_search(self, **kwargs):
        return FakeResult(
            data=[
                {
                    "query": kwargs["queries"][0],
                    "success": True,
                    "results": {
                        "organic": [
                            {
                                "title": "Spectral fusion for apple maturity detection",
                                "link": "https://example.org/paper",
                                "snippet": "A 2025 study of spectral-guided fusion. DOI 10.1234/example.1",
                                "date": "2025-01-01",
                            }
                        ]
                    },
                }
            ]
        )

    def url_crawler(self, **kwargs):
        raise AssertionError("crawler should be disabled in this unit test")


class FakeFailingSearchTools:
    def batch_web_search(self, **kwargs):
        return FakeResult(data=[{"query": kwargs["queries"][0], "success": False, "error": "403 forbidden", "results": []}])


class FakeAcademicSearchTools:
    def academic_search(self, **kwargs):
        return FakeResult(data={
            "results": [{
                "query": kwargs["queries"][0],
                "source": "crossref",
                "success": True,
                "results": [{
                    "title": "Journal article about spectral apple maturity",
                    "authors": "A. Author",
                    "year": "2025",
                    "journal": "Computers and Electronics in Agriculture",
                    "doi": "10.1234/journal.article",
                    "url": "https://doi.org/10.1234/journal.article",
                    "abstract": "Structured publisher metadata.",
                    "source": "crossref",
                }],
            }],
            "warnings": [],
        })

    def batch_web_search(self, **kwargs):
        raise AssertionError("web fallback should not run after successful academic search")

    def url_crawler(self, **kwargs):
        raise AssertionError("crawler should be disabled in this unit test")


class FakeLLMResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class PipelineV2Tests(unittest.TestCase):
    def test_streaming_llm_response_separates_reasoning_from_final_content(self):
        class StreamResponse:
            status_code = 200
            text = ""

            def iter_lines(self, decode_unicode=True):
                yield 'data: {"choices":[{"delta":{"reasoning_content":"private reasoning"}}]}'
                yield 'data: {"choices":[{"delta":{"content":"{\\"overall_score\\":8}"},"finish_reason":"stop"}]}'
                yield "data: [DONE]"

        session = SimpleNamespace(post=lambda **kwargs: StreamResponse())
        with patch("src.utils.llm_client._get_session", return_value=session):
            response = stream_chat_completion_response(
                {"model": "kimi-k2.6", "messages": [{"role": "user", "content": "review"}]},
                model_config={"url": "https://example.test/v1/chat/completions", "token": "x", "provider": "openai_compatible"},
            )
        message = response.json()["choices"][0]["message"]
        self.assertEqual('{"overall_score":8}', message["content"])
        self.assertEqual("private reasoning", message["reasoning_content"])

    def test_streaming_llm_response_decodes_utf8_bytes_for_chinese(self):
        class StreamResponse:
            status_code = 200
            text = ""

            def iter_lines(self, decode_unicode=False):
                self.decode_unicode = decode_unicode
                payload = {"choices": [{"delta": {"content": "融合策略审稿正常"}, "finish_reason": "stop"}]}
                yield ("data: " + json.dumps(payload, ensure_ascii=False)).encode("utf-8")
                yield b"data: [DONE]"

        stream = StreamResponse()
        session = SimpleNamespace(post=lambda **kwargs: stream)
        with patch("src.utils.llm_client._get_session", return_value=session):
            response = stream_chat_completion_response(
                {"model": "kimi-k2.6", "messages": [{"role": "user", "content": "review"}]},
                model_config={"url": "https://example.test/v1/chat/completions", "token": "x", "provider": "openai_compatible"},
            )
        self.assertFalse(stream.decode_unicode)
        self.assertEqual("融合策略审稿正常", response.json()["choices"][0]["message"]["content"])

    def test_extracts_txt_docx_and_xlsx(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            uploads = workspace / "user_uploads"
            uploads.mkdir()
            (uploads / "notes.txt").write_text("苹果成熟度实验数据", encoding="utf-8")

            with zipfile.ZipFile(uploads / "paper.docx", "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>DOCX 方法章节</w:t></w:r></w:p></w:body></w:document>',
                )

            from openpyxl import Workbook

            workbook = Workbook()
            workbook.active.append(["model", "mAP"])
            workbook.active.append(["V2", 0.91])
            workbook.save(uploads / "results.xlsx")

            from reportlab.pdfgen import canvas

            pdf = canvas.Canvas(str(uploads / "evidence.pdf"))
            pdf.drawString(72, 720, "PDF evidence for apple maturity")
            pdf.save()

            sources = collect_source_files(workspace)
            warnings = extract_source_files(sources, workspace)
            self.assertEqual([], warnings)
            content = {source.path.name: source.extracted_text for source in sources}
            self.assertIn("苹果成熟度", content["notes.txt"])
            self.assertIn("DOCX 方法章节", content["paper.docx"])
            self.assertIn("0.91", content["results.xlsx"])
            self.assertIn("PDF evidence", content["evidence.pdf"])
            manifest = json.loads((workspace / "evidence" / "evidence_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(4, len(manifest))

    def test_reference_parser_deduplication_and_indices(self):
        markdown = """# Example paper title

**Source**: Example Journal

## 内容摘要
This 2024 paper reports a verified result. DOI: 10.1000/example.
"""
        parsed = parse_reference_records_from_markdown(markdown, "research/example.md")
        self.assertEqual("Example paper title", parsed[0].title)
        self.assertEqual("2024", parsed[0].year)
        self.assertEqual("10.1000/example", parsed[0].doi)
        duplicate = ReferenceRecord(
            title="Different formatting",
            doi="10.1000/example",
            authors="Complete Author",
            venue="Verified Journal",
            source_type="crossref",
        )
        refs = assign_reference_indices(deduplicate_references(parsed + [duplicate]))
        self.assertEqual(1, len(refs))
        self.assertEqual(1, refs[0].index)
        self.assertEqual("Complete Author", refs[0].authors)
        self.assertEqual("Verified Journal", refs[0].venue)

    def test_reference_download_txt_has_numbered_crlf_lines_and_cited_items_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "research").mkdir(parents=True)
            (workspace / "report").mkdir(parents=True)
            references = [
                {"index": 1, "title": "Paper A", "venue": "Journal A", "doi": "https://doi.org/10.1000/a"},
                {"index": 2, "title": "Paper B", "venue": "Journal B", "doi": "10.1000/b"},
                {"index": 3, "title": "Paper C", "venue": "", "doi": ""},
            ]
            (workspace / "research" / "references.json").write_text(
                json.dumps(references, ensure_ascii=False), encoding="utf-8"
            )
            (workspace / "report" / "final_report.md").write_text(
                "# 正文\n\n证据[2]和补充证据[1]。\n\n# 参考文献\n\n1. ignored\n",
                encoding="utf-8",
            )
            output = write_reference_download_txt(workspace)
            raw = output.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\r\n", raw)
            self.assertEqual(
                [
                    "1. Paper B | 期刊：Journal B | DOI：10.1000/b",
                    "2. Paper A | 期刊：Journal A | DOI：10.1000/a",
                ],
                raw.decode("utf-8-sig").splitlines(),
            )

    def test_empty_source_field_does_not_consume_next_heading(self):
        markdown = "# Paper title\n\n**Source**: \n\n## 内容摘要\nA 2025 abstract."
        parsed = parse_reference_records_from_markdown(markdown, "paper.md")
        self.assertEqual("", parsed[0].venue)
        self.assertNotIn("内容摘要", parsed[0].title)

    def test_online_summary_parser_preserves_authors_year_source_and_abstract(self):
        markdown = """# Valid apple detection paper
**Source**: Computers and Electronics in Agriculture
**Authors**: A. Author, B. Author
**Year**: 2025
**DOI**: 10.1000/apple
**Retrieved via**: crossref
**Query**: apple maturity detection
## Abstract / Evidence
Verified evidence about apple maturity detection.
"""
        parsed = parse_reference_records_from_markdown(markdown, "research/literature_online/001.md")
        self.assertEqual("A. Author, B. Author", parsed[0].authors)
        self.assertEqual("2025", parsed[0].year)
        self.assertEqual("crossref", parsed[0].source_type)
        self.assertIn("Verified evidence", parsed[0].abstract)

    def test_generated_reviews_are_not_loaded_as_references(self):
        self.assertEqual([], parse_reference_records_from_markdown(
            "# Literature Review: apple detection\n\n2025 summary", "research/autonomous_search/literature_review_apple.md"
        ))
        self.assertEqual([], parse_reference_records_from_markdown(
            "# Review for A Lightweight Fusion Strategy\n\n2025 peer review", "research/literature_online/review_for.md"
        ))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "research" / "autonomous_search").mkdir(parents=True)
            (workspace / "research" / "literature_online").mkdir(parents=True)
            (workspace / "research" / "autonomous_search" / "summary.md").write_text(
                "# Fake workflow summary\n\nApple 2025", encoding="utf-8"
            )
            (workspace / "research" / "literature_online" / "paper.md").write_text(
                "# Apple maturity detection study\n**Year**: 2025\n**DOI**: 10.1000/apple", encoding="utf-8"
            )
            loaded = load_existing_references(workspace, query="apple maturity")
        self.assertEqual(["Apple maturity detection study"], [item.title for item in loaded])

    def test_online_retrieval_normalizes_search_results(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"V2_CRAWL_MAX_SOURCES": "0", "V2_MIN_VALID_REFERENCES": "0"}):
            refs, warnings = retrieve_online_references(
                [LiteratureTask(topic="test", queries=["apple spectral fusion"])],
                FakeSearchTools(),
                Path(tmp),
            )
            self.assertEqual([], warnings)
            self.assertEqual(1, len(refs))
            self.assertEqual("2025", refs[0].year)
            self.assertEqual("10.1234/example.1", refs[0].doi)
            self.assertTrue((Path(tmp) / "research" / "search_results_raw.json").exists())
            self.assertTrue((Path(tmp) / refs[0].source_path).exists())
            self.assertIn("literature_online", refs[0].source_path)

    def test_online_retrieval_surfaces_per_query_failures(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"V2_CRAWL_MAX_SOURCES": "0", "V2_MIN_VALID_REFERENCES": "0"}):
            refs, warnings = retrieve_online_references(
                [LiteratureTask(topic="test", queries=["query"])],
                FakeFailingSearchTools(),
                Path(tmp),
            )
            self.assertEqual([], refs)
            self.assertTrue(any("403 forbidden" in warning for warning in warnings))

    def test_structured_academic_search_precedes_web_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"V2_CRAWL_MAX_SOURCES": "0", "V2_ACADEMIC_SEARCH": "true", "V2_ALWAYS_WEB_SEARCH": "false", "V2_MIN_VALID_REFERENCES": "0"},
        ):
            refs, warnings = retrieve_online_references(
                [LiteratureTask(topic="test", queries=["apple spectral maturity"])],
                FakeAcademicSearchTools(),
                Path(tmp),
            )
            self.assertEqual([], warnings)
            self.assertEqual(1, len(refs))
            self.assertEqual("crossref", refs[0].source_type)
            self.assertEqual("10.1234/journal.article", refs[0].doi)

    def test_academic_search_mcp_schema_and_optional_sciencedirect_failure(self):
        self.assertIn("academic_search", get_tool_schemas())
        with patch.dict(os.environ, {"ELSEVIER_API_KEY": ""}, clear=False):
            self.assertEqual(["crossref", "openalex"], configured_sources("crossref,openalex,unknown"))
            blocks, warnings = search_academic_sources(
                ["apple maturity"], sources=["sciencedirect"], max_results_per_query=1
            )
        self.assertEqual([], blocks)
        self.assertEqual([], warnings)

    def test_citation_audit_detects_invalid_numbers(self):
        refs = [ReferenceRecord(title="A", index=1), ReferenceRecord(title="B", index=2)]
        audit = audit_citations("# 正文\n结论由文献[1]和错误文献[9]支持。\n# 参考文献", refs)
        self.assertEqual([1], audit["cited_reference_indices"])
        self.assertEqual([9], audit["out_of_range_citations"])
        self.assertFalse(audit["passed"])

    def test_citations_are_pruned_and_renumbered_from_structured_catalogue(self):
        refs = [
            ReferenceRecord(title="Apple imaging", authors="A", year="2024", venue="Journal A", index=1),
            ReferenceRecord(title="Unrelated record", authors="B", year="2022", venue="Journal B", index=2),
            ReferenceRecord(title="Spectral fusion", authors="C", year="2025", venue="Journal C", index=3),
        ]
        manuscript = "# 正文\n光谱融合已有研究[3]，苹果成像也有研究[1]。\n# 参考文献\n[1] wrong\n[3] wrong\n"
        synchronized, selected = synchronize_citations(manuscript, refs)
        self.assertEqual(["Spectral fusion", "Apple imaging"], [item.title for item in selected])
        self.assertIn("光谱融合已有研究[1]，苹果成像也有研究[2]", synchronized)
        self.assertNotIn("Unrelated record", synchronized)
        audit = audit_citations(synchronized, selected)
        self.assertTrue(audit["passed"])
        self.assertEqual([], audit["uncited_reference_indices"])

    def test_citation_audit_does_not_count_bibliography_numbers_as_body_citations(self):
        refs = [
            ReferenceRecord(title="A", authors="Author", year="2024", venue="Journal", index=1),
            ReferenceRecord(title="B", authors="Author", year="2024", venue="Journal", index=2),
        ]
        audit = audit_citations("# 正文\n正文只引用[1]。\n# 参考文献\n[1] A\n[2] B", refs)
        self.assertEqual([1], audit["cited_reference_indices"])
        self.assertEqual([2], audit["uncited_reference_indices"])
        self.assertFalse(audit["passed"])

    def test_reviewer_model_config_uses_role_specific_env_without_fallback(self):
        env = {
            "METHOD_REVIEWER_URL": "https://method.example/v1/chat/completions",
            "METHOD_REVIEWER_API_KEY": "method-secret",
            "METHOD_REVIEWER_MODEL": "method-model",
            "MODEL_REQUEST_URL": "https://writer.example/v1/chat/completions",
            "MODEL_NAME": "writer-model",
        }
        with patch.dict(os.environ, env, clear=False):
            config = get_reviewer_model_config("methodology")
        self.assertEqual("https://method.example/v1/chat/completions", config["url"])
        self.assertEqual("method-secret", config["token"])
        self.assertEqual("method-model", config["model"])
        self.assertEqual("openai_compatible", config["provider"])
        self.assertEqual("dedicated_reviewer", config["model_source"])

    def test_incomplete_reviewer_config_falls_back_to_primary_model_per_role(self):
        env = {
            "METHOD_REVIEWER_URL": "",
            "METHOD_REVIEWER_API_KEY": "",
            "METHOD_REVIEWER_MODEL": "",
            "MODEL_REQUEST_URL": "https://primary.example/v1/chat/completions",
            "MODEL_REQUEST_TOKEN": "primary-secret",
            "MODEL_NAME": "qwen-primary",
            "MODEL_PROVIDER": "openai_compatible",
            "V2_REVIEW_FALLBACK_TO_PRIMARY": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            config = get_reviewer_model_config("methodology")
        self.assertEqual("https://primary.example/v1/chat/completions", config["url"])
        self.assertEqual("primary-secret", config["token"])
        self.assertEqual("qwen-primary", config["model"])
        self.assertEqual("primary_model_fallback", config["model_source"])

    def test_kimi_k26_uses_required_temperature(self):
        with patch.dict(os.environ, {"ADVERSARIAL_REVIEWER_TEMPERATURE": ""}, clear=False):
            self.assertIsNone(_reviewer_temperature("adversarial", "kimi-k2.6"))
            self.assertGreaterEqual(_reviewer_max_tokens("adversarial", "kimi-k2.6"), 16000)

    def test_method_reviewer_has_headroom_and_retries_invalid_json_compactly(self):
        env = {
            "METHOD_REVIEWER_URL": "https://method.example/v1/chat/completions",
            "METHOD_REVIEWER_API_KEY": "method-key",
            "METHOD_REVIEWER_MODEL": "MiniMax-M2.7",
            "METHOD_REVIEWER_MAX_TOKENS": "4096",
        }
        calls = []

        def fake_chat(payload, **kwargs):
            calls.append(payload)
            if len(calls) == 1:
                return FakeLLMResponse({
                    "choices": [{"message": {"content": '{"overall_score": 6, "decision": "Major'}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 4096},
                })
            content = json.dumps({
                "role": "methodology", "overall_score": 6, "decision": "大修",
                "assessment_boundary": "仅检查提供的证据", "critical_issues": [], "major_issues": ["需要澄清方法细节"],
                "minor_issues": [], "citation_issues": [], "strengths": [], "revision_priorities": ["补充实现细节"],
                "unsupported_claims": [], "evidence_checked": ["已检查研究契约"], "axis_scores": {"technical_soundness": 6},
            })
            return FakeLLMResponse({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=False), \
                patch("src.pipeline_v2.review.chat_completion_response", side_effect=fake_chat):
            review = _review_one(PipelineContext(workspace_path=Path(tmp), query="paper"), "# Paper", "methodology")
        self.assertGreaterEqual(calls[0]["max_tokens"], 8192)
        self.assertEqual(2, len(calls))
        self.assertIn("previous review was truncated", calls[1]["messages"][-1]["content"])
        self.assertEqual("completed", review["status"])

    def test_four_reviewers_use_four_independent_model_configs(self):
        env = {
            "METHOD_REVIEWER_URL": "https://method.example/v1/chat/completions",
            "METHOD_REVIEWER_API_KEY": "method-key",
            "METHOD_REVIEWER_MODEL": "method-model",
            "EXPERIMENT_REVIEWER_URL": "https://experiment.example/v1/chat/completions",
            "EXPERIMENT_REVIEWER_API_KEY": "experiment-key",
            "EXPERIMENT_REVIEWER_MODEL": "experiment-model",
            "CITATION_REVIEWER_URL": "https://citation.example/v1/chat/completions",
            "CITATION_REVIEWER_API_KEY": "citation-key",
            "CITATION_REVIEWER_MODEL": "citation-model",
            "ADVERSARIAL_REVIEWER_URL": "https://adversarial.example/v1/chat/completions",
            "ADVERSARIAL_REVIEWER_API_KEY": "adversarial-key",
            "ADVERSARIAL_REVIEWER_MODEL": "adversarial-model",
            "V2_REVIEW_WORKERS": "4",
        }
        calls = []

        def fake_chat(payload, *, model_config, agent_name, **kwargs):
            calls.append((agent_name, model_config["url"], model_config["model"], payload["model"]))
            role = agent_name.rsplit("_", 1)[-1]
            if "experiment_evidence" in agent_name:
                role = "experiment_evidence"
            content = json.dumps({
                "role": role,
                "overall_score": 8,
                "decision": "大修",
                "assessment_boundary": "仅检查已提供的证据",
                "critical_issues": [],
                "major_issues": ["需要核对实验证据"],
                "minor_issues": [],
                "citation_issues": [],
                "strengths": ["研究问题清晰"],
                "revision_priorities": ["解决证据缺口"],
                "unsupported_claims": [],
                "evidence_checked": ["已检查研究契约"],
                "axis_scores": {"technical_soundness": 8},
            })
            return FakeLLMResponse({"choices": [{"message": {"content": content}}]})

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, env, clear=False), \
             patch("src.pipeline_v2.review.chat_completion_response", side_effect=fake_chat):
            ctx = PipelineContext(workspace_path=Path(tmp), query="paper")
            reviews, report_path, warnings = run_reviews(ctx, "# Manuscript", Path(tmp))
            self.assertEqual([], warnings)
            self.assertTrue(report_path.exists())
            self.assertTrue((Path(tmp) / "peer_review_synthesis.json").exists())
            self.assertEqual(list(REVIEW_ORDER), [review["role"] for review in reviews])
            self.assertTrue(all(review["status"] == "completed" for review in reviews))
        self.assertEqual(4, len(calls))
        self.assertEqual(
            {"method-model", "experiment-model", "citation-model", "adversarial-model"},
            {call[2] for call in calls},
        )

    def test_missing_reviewer_env_is_explicit_failure_not_writer_fallback(self):
        reviewer_keys = {
            f"{prefix}_{suffix}": ""
            for prefix in ("METHOD_REVIEWER", "EXPERIMENT_REVIEWER", "CITATION_REVIEWER", "ADVERSARIAL_REVIEWER")
            for suffix in ("URL", "API_KEY", "MODEL")
        }
        reviewer_keys["V2_REVIEW_FALLBACK_TO_PRIMARY"] = "false"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, reviewer_keys, clear=False):
            ctx = PipelineContext(workspace_path=Path(tmp), query="paper")
            reviews, _, warnings = run_reviews(ctx, "# Manuscript", Path(tmp))
        self.assertEqual(4, len(warnings))
        self.assertTrue(all(review["status"] == "failed" for review in reviews))
        self.assertTrue(all(review["overall_score"] is None for review in reviews))

    def test_plan_only_builds_evidence_and_reference_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            uploads = workspace / "user_uploads"
            uploads.mkdir()
            (uploads / "notes.txt").write_text("apple maturity YOLO spectral fusion", encoding="utf-8")
            result = PipelineV2(workspace).run(
                "请根据材料写论文",
                make_pdf=False,
                plan_only=True,
                use_web_search=False,
                enable_review=False,
            )
            self.assertTrue(result.success)
            self.assertTrue((workspace / "pipeline_state_initial.json").exists())
            self.assertTrue((workspace / "research" / "literature_plan.md").exists())
            self.assertTrue((workspace / "research" / "references.json").exists())
            self.assertTrue((workspace / "research" / "research_contract.json").exists())
            self.assertTrue((workspace / "research" / "claims_evidence.json").exists())
            self.assertTrue((workspace / "research" / "paper_outline.json").exists())

    def test_zip_experiment_folder_is_safely_extracted_and_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            uploads = workspace / "user_uploads"
            uploads.mkdir()
            archive_path = uploads / "experiments.zip"
            csv_text = (
                "epoch,metrics/precision(B),metrics/recall(B),metrics/mAP50(B),metrics/mAP50-95(B)\n"
                "0,0.70,0.65,0.72,0.50\n"
                "1,0.80,0.75,0.84,0.62\n"
            )
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("总说明.txt", "所有实验使用同一个苹果数据集和训练设置。")
                archive.writestr("完整光谱模型/yolo26+spec.csv", csv_text)
                archive.writestr("完整光谱模型/说明.txt", "这是包含光谱分支和交叉注意力的完整模型。")
            result = PipelineV2(workspace).run(
                "根据实验结果撰写论文", make_pdf=False, plan_only=True, use_web_search=False, enable_review=False
            )
            self.assertTrue(result.success)
            registry = json.loads((workspace / "experiment_results" / "experiment_registry.json").read_text(encoding="utf-8"))
            self.assertEqual(1, len(registry))
            self.assertTrue(registry[0]["display_name"].endswith("/ yolo26+spec"))
            self.assertEqual(1, registry[0]["best_epoch"])
            self.assertAlmostEqual(0.62, registry[0]["best_validation_metrics"]["mAP50-95"])
            self.assertFalse(registry[0]["needs_user_confirmation"])
            self.assertIn("同一个苹果数据集", registry[0]["description"])
            self.assertIn("完整模型", registry[0]["description"])
            self.assertEqual(2, len(registry[0]["description_files"]))
            self.assertTrue(any(path.endswith("training_curves.png") for path in registry[0]["figures"]))
            for figure_path in registry[0]["figures"]:
                self.assertTrue((workspace / figure_path).exists())

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            uploads = workspace / "user_uploads"
            uploads.mkdir()
            with zipfile.ZipFile(uploads / "unsafe.zip", "w") as archive:
                archive.writestr("../escape.txt", "unsafe")
            result = PipelineV2(workspace).run(
                "plan", make_pdf=False, plan_only=True, use_web_search=False, enable_review=False
            )
            self.assertTrue(result.success)
            self.assertFalse((workspace.parent / "escape.txt").exists())
            self.assertTrue(any("safely extract" in warning for warning in result.warnings))

    def test_task_manager_pause_intervention_and_resume(self):
        manager = TaskManager()
        manager.create_task("task-1", "paper")
        manager.update_task_status("task-1", TaskStatus.RUNNING)
        self.assertTrue(manager.request_pause("task-1"))
        output = []

        def checkpoint_worker():
            output.extend(manager.checkpoint("task-1", "research_contract_ready"))

        thread = threading.Thread(target=checkpoint_worker)
        thread.start()
        time.sleep(0.1)
        self.assertEqual(TaskStatus.PAUSED, manager.get_task("task-1").status)
        self.assertTrue(manager.add_intervention("task-1", "后续重点分析光谱分支"))
        self.assertTrue(manager.resume_task("task-1"))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["后续重点分析光谱分支"], output)
        self.assertEqual(TaskStatus.RUNNING, manager.get_task("task-1").status)

    def test_task_manager_routes_stage_switch_and_keeps_it_until_applied(self):
        manager = TaskManager()
        manager.create_task("directive-1", "paper")
        manager.update_task_status("directive-1", TaskStatus.RUNNING)

        self.assertTrue(manager.add_intervention("directive-1", "当前检索完成后直接进入实验环节"))
        self.assertEqual("experiment", manager.peek_requested_stage("directive-1")["stage"])
        self.assertEqual(
            ["当前检索完成后直接进入实验环节"],
            manager.checkpoint("directive-1", "information_seeker_tool_boundary"),
        )
        self.assertEqual("experiment", manager.peek_requested_stage("directive-1")["stage"])
        self.assertTrue(manager.clear_requested_stage("directive-1", "experiment"))
        self.assertIsNone(manager.peek_requested_stage("directive-1"))
        self.assertTrue(any(event["type"] == "guidance_applied" for event in manager.get_events("directive-1")))

    def test_task_manager_keeps_keyword_guidance_local_without_stage_switch(self):
        manager = TaskManager()
        manager.create_task("guidance-1", "paper")
        manager.update_task_status("guidance-1", TaskStatus.RUNNING)
        instruction = "下一轮增加关键词 AS7265x calibration 和 apple maturity"

        self.assertTrue(manager.add_intervention("guidance-1", instruction))
        self.assertIsNone(manager.peek_requested_stage("guidance-1"))
        self.assertEqual([instruction], manager.checkpoint("guidance-1", "information_seeker_iteration"))

    def test_planner_skips_new_search_batch_when_user_requests_experiment(self):
        manager = get_task_manager()
        task_id = "planner-stage-directive-test"
        manager.remove_task(task_id)
        manager.create_task(task_id, "paper")
        manager.update_task_status(task_id, TaskStatus.RUNNING)
        manager.add_intervention(task_id, "不用继续搜索，直接进入实验环节")
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.logger = logging.getLogger("planner-stage-directive-test")
        planner.task_id = task_id
        planner._information_seeker_completed = False
        planner._publish_agent_progress = lambda *args, **kwargs: None

        try:
            result = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=[{
                "task_content": "Search additional broad apple maturity literature."
            }])
        finally:
            manager.remove_task(task_id)

        self.assertTrue(result["success"])
        self.assertEqual("stopped_by_user_guidance", result["data"]["completion_status"])
        self.assertEqual("experiment", result["metadata"]["requested_stage"])

    def test_task_manager_persists_structured_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TaskManager()
            manager.create_task("persist-1", "paper")
            event_path = Path(tmp) / "workflow_events.jsonl"
            self.assertTrue(manager.set_event_log_path("persist-1", str(event_path)))
            manager.record_event("persist-1", "research_activity", "检索文献", {"stage": "literature_activity"})
            saved = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("research_activity", saved[0]["type"])
            self.assertEqual(1, saved[0]["id"])

    def test_research_page_is_separate_from_legacy_chat_page(self):
        legacy = PROJECT_ROOT / "chatAi" / "ai_chat.html"
        research = PROJECT_ROOT / "chatAi" / "research.html"
        self.assertTrue(research.exists())
        self.assertNotIn("research-workflow-panel", legacy.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("research-workflow-panel", research.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("research-live-draft", research.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("research-preview-panel", research.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("research-chapter-nav", research.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("research-workflow-header", research.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("hybridStages", research.read_text(encoding="utf-8", errors="ignore"))
        self.assertIn("research-agent-activity", research.read_text(encoding="utf-8", errors="ignore"))
        research_text = research.read_text(encoding="utf-8", errors="ignore")
        self.assertIn("research-chat-stream", research_text)
        self.assertIn("writer_paragraph_ready", research_text)
        self.assertIn("lastEventId", research_text)
        self.assertIn("#research-preview-panel { display: none", research_text)

    def test_research_contract_marks_missing_original_research_evidence(self):
        source = SourceFile(
            path=Path("notes.txt"),
            rel_path="user_uploads/notes.txt",
            kind="text",
            size_bytes=20,
            extracted_text="本文提出一个新的融合模型。",
        )
        contract, claims, outline = build_research_contract("请撰写原创实验论文", [source], [])
        self.assertEqual("original_research", contract.paper_type)
        self.assertFalse(contract.evidence_sufficient)
        self.assertTrue(any("实验结果" in item for item in contract.missing_evidence))
        self.assertTrue(any(claim.claim_type == "method" for claim in claims))
        self.assertIn("experiments", outline)

    def test_compact_citations_removes_uncited_references_and_renumbers(self):
        ctx = PipelineContext(workspace_path=Path("."), query="paper")
        ctx.references = [
            ReferenceRecord(title="A", index=1),
            ReferenceRecord(title="B", index=2),
            ReferenceRecord(title="C", index=3),
        ]
        ctx.section_outputs = {"introduction": "已有研究[1, 3]支持该问题。"}
        compact_citations(ctx)
        self.assertEqual(["A", "C"], [reference.title for reference in ctx.references])
        self.assertEqual("已有研究[1, 2]支持该问题。", ctx.section_outputs["introduction"])

    def test_full_pipeline_writes_review_and_citation_artifacts_with_mocked_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            uploads = workspace / "user_uploads"
            uploads.mkdir()
            (uploads / "notes.txt").write_text("apple maturity verified metric mAP=0.91", encoding="utf-8")

            online_ref = ReferenceRecord(
                title="Verified apple maturity paper",
                year="2025",
                url="https://example.org/verified",
                evidence="Supports apple maturity detection.",
                source_type="web_search",
            )

            def fake_section(ctx, section):
                return f"### {section.title}\n\n本节仅使用用户证据[S1]和已提供文献[1]。"

            def fake_reviews(ctx, manuscript, report_dir):
                review_path = report_dir / "peer_review.md"
                review_path.write_text("# review\n", encoding="utf-8")
                reviews = [{
                    "role": "methodology",
                    "overall_score": 8.0,
                    "critical_issues": [],
                    "major_issues": [],
                    "minor_issues": [],
                    "citation_issues": [],
                }]
                return reviews, review_path, []

            with patch("src.pipeline_v2.runner.retrieve_online_references", return_value=([online_ref], [])), \
                 patch("src.pipeline_v2.runner.call_llm_for_section", side_effect=fake_section), \
                 patch("src.pipeline_v2.runner.run_reviews", side_effect=fake_reviews):
                result = PipelineV2(workspace).run(
                    "apple maturity paper",
                    make_pdf=False,
                    use_web_search=True,
                    enable_review=True,
                    auto_revise=True,
                )

            self.assertTrue(result.success)
            self.assertTrue((workspace / "report" / "final_report.md").exists())
            self.assertTrue((workspace / "report" / "reference_download_list.txt").exists())
            self.assertTrue(os.path.samefile(
                workspace / "report" / "reference_download_list.txt",
                result.reference_download_path,
            ))
            final_text = (workspace / "report" / "final_report.md").read_text(encoding="utf-8")
            self.assertIn("# 标题、摘要与关键词", final_text)
            self.assertTrue((workspace / "report" / "peer_review.md").exists())
            audit = json.loads((workspace / "report" / "citation_audit.json").read_text(encoding="utf-8"))
            self.assertEqual([], audit["out_of_range_citations"])


    def test_figure_planner_handles_regression_and_grouped_machine_learning_data(self):
        regression_rows = [
            {"observed": "1.0", "predicted": "1.1", "temperature": "20"},
            {"observed": "2.0", "predicted": "1.9", "temperature": "25"},
        ]
        with patch.dict(os.environ, {"FIGURE_AGENT_USE_PRIMARY_MODEL": "false"}, clear=False):
            regression = build_figure_plan(
                list(regression_rows[0]), regression_rows, {}, [], "mathematical model prediction"
            )
        self.assertEqual("actual_vs_predicted", regression["figures"][0]["chart_type"])

        grouped_rows = [
            {"method": "A", "score": "0.8"}, {"method": "A", "score": "0.82"},
            {"method": "B", "score": "0.9"}, {"method": "B", "score": "0.88"},
        ]
        with patch.dict(os.environ, {"FIGURE_AGENT_USE_PRIMARY_MODEL": "false"}, clear=False):
            grouped = build_figure_plan(list(grouped_rows[0]), grouped_rows, {}, [], "machine learning comparison")
        self.assertTrue(any(item["chart_type"] == "box" for item in grouped["figures"]))

    def test_figure_critic_rejects_bar_that_hides_repeated_observations(self):
        review = audit_figure(
            {"chart_type": "bar", "x": "method", "y": ["score"]},
            {"row_count": 4, "numeric_columns": ["score"], "categorical_columns": ["method"],
             "cardinality": {"method": 2}},
            [], Path("."),
        )
        self.assertFalse(review["passed"])
        self.assertTrue(any("Repeated observations" in issue["message"] for issue in review["issues"]))

    def test_hybrid_brief_preserves_autonomous_loop_with_v2_constraints(self):
        brief = build_autonomous_agent_brief(Path("workspace"), "write a complete paper")
        self.assertIn("PlannerAgent -> InformationSeeker/ExperimentAgent -> WriterAgent", brief)
        self.assertIn("assign_multi_subjective_tasks_to_info_seeker", brief)
        self.assertIn("assign_task_to_experimenter", brief)
        self.assertIn("MUST NOT be reprocessed", brief)
        self.assertIn("bibliography merging", brief)
        self.assertIn("research/claims_evidence.json", brief)
        self.assertIn("Search iteratively", brief)
        self.assertIn("evidence_gap", brief)
        self.assertIn("report/final_report.md", brief)

    def test_chinese_query_keeps_english_reference_with_workspace_search_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            literature_dir = workspace / "research" / "literature_online"
            literature_dir.mkdir(parents=True)
            (literature_dir / "paper.md").write_text(
                "# Lightweight apple ripeness detection using multispectral fusion\n\n"
                "**Source**: Computers and Electronics in Agriculture\n"
                "**Authors**: Example Author\n"
                "**Year**: 2025\n"
                "**DOI**: 10.1000/example\n"
                "**URL**: https://doi.org/10.1000/example\n"
                "**Retrieved via**: crossref\n"
                "**Query**: apple ripeness spectral cross attention lightweight detection\n\n"
                "## Abstract / Evidence\n\nMultispectral evidence for fruit maturity detection.\n",
                encoding="utf-8",
            )

            references = load_existing_references(
                workspace,
                query="基于光谱引导跨注意力融合的轻量化苹果成熟度无损检测",
                include_bundled=False,
            )

            self.assertEqual(1, len(references))
            self.assertEqual("10.1000/example", references[0].doi)

    def test_planner_identifies_reference_management_as_non_experiment_work(self):
        self.assertTrue(PlannerAgent._is_reference_management_task(
            "Create a consolidated literature references file and merge all DOIs."
        ))
        self.assertFalse(PlannerAgent._is_reference_management_task(
            "Analyze all experimental CSV data and calculate ablation metrics."
        ))

    def test_verified_registry_is_reused_for_unchanged_bulk_experiment_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "experiment_results" / "uploaded" / "run.csv"
            source.parent.mkdir(parents=True)
            source.write_text("epoch,score\n1,0.8\n", encoding="utf-8")
            digest = PlannerAgent._sha256_file(source)
            registry = [{
                "experiment_id": "EXP0001",
                "results_csv": "experiment_results/uploaded/run.csv",
                "sha256": digest,
                "status": "processed",
                "figures": ["experiment_results/figures/EXP0001/training_curves.png"],
            }]
            (workspace / "experiment_results" / "experiment_registry.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )

            planner = PlannerAgent.__new__(PlannerAgent)
            planner.logger = logging.getLogger("planner-experiment-reuse-test")
            planner.mcp_tools = SimpleNamespace(workspace_path=str(workspace))
            planner.task_id = None
            planner._experiment_agent_completed = False
            planner._successful_experiment_task_fingerprints = set()
            planner._publish_agent_progress = lambda *args, **kwargs: None

            result = planner.assign_task_to_experimenter(
                "Process all experimental CSV data and create a comprehensive experiment analysis."
            )

            self.assertTrue(result["success"])
            self.assertEqual("reused_verified_registry", result["data"]["completion_status"])
            self.assertTrue(planner._experiment_agent_completed)

    def test_hybrid_completion_state_is_restored_from_verified_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "experiment_results" / "uploaded" / "run.csv"
            source.parent.mkdir(parents=True)
            source.write_text("epoch,score\n1,0.8\n", encoding="utf-8")
            registry = [{
                "experiment_id": "EXP0001",
                "results_csv": "experiment_results/uploaded/run.csv",
                "sha256": PlannerAgent._sha256_file(source),
                "status": "processed",
            }]
            (workspace / "experiment_results" / "experiment_registry.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )

            planner = PlannerAgent.__new__(PlannerAgent)
            planner.logger = logging.getLogger("planner-hybrid-state-restore-test")
            planner.mcp_tools = SimpleNamespace(workspace_path=str(workspace))
            planner._information_seeker_completed = False
            planner._experiment_agent_completed = False
            planner._writer_agent_completed = False

            gate = {
                "reference_count": 30,
                "minimum_reference_count": 30,
                "reference_gate_met": True,
            }
            with patch.dict(os.environ, {"SCIA_PIPELINE_VERSION": "hybrid"}), patch(
                "src.pipeline_v2.hybrid.refresh_hybrid_evidence", return_value=gate,
            ):
                state = planner._sync_hybrid_completion_from_artifacts(
                    verify_literature=True, query="paper",
                )

            self.assertEqual(1, state["experiment_count"])
            self.assertEqual(gate, state["reference_gate"])
            self.assertTrue(planner._experiment_agent_completed)
            self.assertFalse(planner._information_seeker_completed)
            self.assertFalse(planner._writer_agent_completed)

    def test_local_reference_preparation_never_runs_online_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            events = []
            with patch("src.pipeline_v2.hybrid.retrieve_online_references") as online_search:
                count, _ = seed_hybrid_literature(
                    workspace,
                    "中文科研题目",
                    enabled=False,
                    checkpoint=lambda stage, data: events.append((stage, data)) or [],
                )

            self.assertEqual(0, count)
            online_search.assert_not_called()
            self.assertEqual("local_reference_inventory_started", events[0][0])
            self.assertEqual("local_reference_inventory_ready", events[-1][0])
            self.assertFalse(events[-1][1]["online_search"])

    def test_new_nested_csv_invalidates_verified_registry_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "experiment_results" / "uploaded" / "run.csv"
            source.parent.mkdir(parents=True)
            source.write_text("epoch,score\n1,0.8\n", encoding="utf-8")
            registry = [{
                "results_csv": "experiment_results/uploaded/run.csv",
                "sha256": PlannerAgent._sha256_file(source),
                "status": "processed",
            }]
            (workspace / "experiment_results" / "experiment_registry.json").write_text(
                json.dumps(registry), encoding="utf-8"
            )
            (workspace / "experiment_results" / "uploaded" / "new_run.csv").write_text(
                "epoch,score\n1,0.9\n", encoding="utf-8"
            )

            self.assertEqual([], PlannerAgent._load_reusable_experiment_registry(workspace))

    def test_planner_init_registers_old_flow_delegation_tools(self):
        def fake_base_init(agent, config, shared_mcp_client=None):
            agent.config = config
            agent.available_tools = {}
            agent.logger = logging.getLogger("planner-init-test")

        with patch.object(BaseAgent, "__init__", fake_base_init), \
                patch.object(PlannerAgent, "_build_tool_schemas", return_value=[]):
            planner = PlannerAgent(AgentConfig(agent_name="PlannerAgent"))

        self.assertIn("assign_multi_subjective_tasks_to_info_seeker", planner.available_tools)
        self.assertIn("assign_task_to_experimenter", planner.available_tools)
        self.assertIn("assign_subjective_task_to_writer", planner.available_tools)

    def test_planner_deduplicates_repeated_broad_literature_topics(self):
        broad = {
            "task_content": (
                "Search literature and papers about apple maturity, spectral fusion, cross-attention, "
                "wavelet, Ghost, YOLOv5, YOLOv8, YOLOv10, YOLOv11, YOLO26, agriculture and fruit citations."
            )
        }
        normalized = PlannerAgent._normalize_information_tasks([broad, dict(broad)])
        self.assertEqual(3, len(normalized))
        self.assertEqual(3, len({PlannerAgent._information_task_fingerprint(task) for task in normalized}))

    def test_planner_blocks_a_successful_information_batch_from_running_again(self):
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.logger = logging.getLogger("planner-info-dedup-test")
        planner._information_seeker_completed = True
        planner._successful_information_task_fingerprints = set()
        planner._successful_information_task_texts = []
        tasks = [{"task_content": "Search papers about apple maturity detection with YOLO."}]
        fingerprint = PlannerAgent._information_batch_fingerprint(tasks)
        planner._successful_information_batch_fingerprints = {fingerprint}

        result = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=tasks)

        self.assertTrue(result["success"])
        self.assertEqual("already_completed", result["data"]["completion_status"])
        self.assertTrue(result["metadata"]["duplicate_batch_blocked"])

    def test_planner_requires_a_concrete_gap_for_supplemental_search(self):
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.logger = logging.getLogger("planner-info-gap-test")
        planner._information_seeker_completed = True
        planner._successful_information_task_fingerprints = set()
        planner._successful_information_task_texts = []
        planner._successful_information_batch_fingerprints = set()

        result = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=[{
            "task_content": "Search recent calibration studies for multispectral fruit sensors."
        }])

        self.assertFalse(result["success"])
        self.assertIn("evidence_gap", result["error"])

    def test_planner_allows_targeted_supplemental_search_with_evidence_gap(self):
        import sys

        planner = PlannerAgent.__new__(PlannerAgent)
        planner.logger = logging.getLogger("planner-info-gap-allowed-test")
        planner.config = AgentConfig(agent_name="PlannerAgent", model="test-model")
        planner.task_id = "task-test"
        planner.sub_agent_configs = {}
        planner._information_seeker_completed = True
        planner._successful_information_task_fingerprints = set()
        planner._successful_information_task_texts = []
        planner._successful_information_batch_fingerprints = set()
        planner._cancellation_token = None
        planner.mcp_tools = SimpleNamespace(client=object(), workspace_path=str(DEEPDIVER_ROOT))
        planner._check_cancellation = lambda: False
        planner._publish_agent_progress = lambda *args, **kwargs: None

        response = SimpleNamespace(success=True, result={"papers": 2}, iterations=2, error=None)
        fake_agent = SimpleNamespace(task_id=None, execute_task=lambda task: response)
        fake_agents_module = SimpleNamespace(
            TaskInput=lambda **kwargs: SimpleNamespace(**kwargs),
            create_subjective_information_seeker=lambda **kwargs: fake_agent,
        )
        task = {
            "task_content": "Search validation papers for AS7265x sensor calibration in fruit studies.",
            "evidence_gap": "The sensor calibration claim has no peer-reviewed source from 2021-2025.",
        }

        with patch.dict(sys.modules, {"agents": fake_agents_module}), patch.dict(
                os.environ, {"SCIA_PIPELINE_VERSION": "test", "INFO_SEEKER_TASK_DELAY_SECONDS": "0"}
        ):
            first = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=[task])
            second = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=[task])

        self.assertTrue(first["success"])
        self.assertEqual(1, first["metadata"]["success_count"])
        self.assertTrue(second["success"])
        self.assertEqual("already_completed", second["data"]["completion_status"])

    def test_planner_retries_only_failed_information_subtask(self):
        import sys

        planner = PlannerAgent.__new__(PlannerAgent)
        planner.logger = logging.getLogger("planner-info-failed-only-test")
        planner.config = AgentConfig(agent_name="PlannerAgent", model="test-model")
        planner.task_id = None
        planner.sub_agent_configs = {}
        planner._information_seeker_completed = False
        planner._successful_information_task_fingerprints = set()
        planner._successful_information_task_texts = []
        planner._successful_information_batch_fingerprints = set()
        planner._failed_information_tasks = {}
        planner._cancellation_token = None
        planner.mcp_tools = SimpleNamespace(client=object(), workspace_path=str(DEEPDIVER_ROOT))
        planner._check_cancellation = lambda: False
        planner._publish_agent_progress = lambda *args, **kwargs: None

        executions = []
        failed_attempts = 0

        def execute(task):
            nonlocal failed_attempts
            executions.append(task.task_content)
            if "architecture-failure-marker" in task.task_content:
                failed_attempts += 1
                if failed_attempts == 1:
                    return SimpleNamespace(success=False, result=None, iterations=2, error="temporary formatting failure")
            return SimpleNamespace(success=True, result={"papers": 3}, iterations=2, error=None)

        def create_agent(**kwargs):
            return SimpleNamespace(task_id=None, execute_task=execute)

        fake_agents_module = SimpleNamespace(
            TaskInput=lambda **kwargs: SimpleNamespace(**kwargs),
            create_subjective_information_seeker=create_agent,
        )
        initial = [
            {"task_content": "Survey apple maturity detection evidence from agricultural journals."},
            {"task_content": "Collect authoritative YOLO architecture-failure-marker component sources."},
        ]
        planner_retry_batch = [
            {"task_content": "Survey apple maturity detection evidence from agricultural journals."},
            {"task_content": "Collect authoritative YOLO architecture-failure-marker component sources."},
            {"task_content": "Find unrelated sensor calibration standards.", "evidence_gap": "new gap"},
        ]

        with patch.dict(sys.modules, {"agents": fake_agents_module}), patch.dict(
                os.environ, {
                    "SCIA_PIPELINE_VERSION": "test", "INFO_SEEKER_TASK_DELAY_SECONDS": "0",
                    "INFO_SEEKER_FAILED_TASK_MAX_RETRIES": "1",
                }
        ):
            first = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=initial)
            second = planner.assign_multi_subjective_tasks_to_info_seeker(tasks=planner_retry_batch)

        self.assertFalse(first["success"])
        self.assertTrue(second["success"])
        self.assertTrue(second["metadata"]["retry_only_failed_tasks"])
        self.assertEqual(1, executions.count("Survey apple maturity detection evidence from agricultural journals."))
        self.assertEqual(2, executions.count("Collect authoritative YOLO architecture-failure-marker component sources."))
        self.assertNotIn("Find unrelated sensor calibration standards.", executions)
        self.assertEqual({}, planner._failed_information_tasks)

    def test_hybrid_writer_completion_survives_legacy_review_skip(self):
        planner = PlannerAgent.__new__(PlannerAgent)
        planner.logger = logging.getLogger("planner-writer-state-test")
        planner.config = AgentConfig(agent_name="PlannerAgent", model="test-model")
        planner.task_id = "task-test"
        planner.sub_agent_configs = {}
        planner._information_seeker_completed = True
        planner._experiment_agent_completed = True
        planner._writer_agent_completed = False
        planner._cancellation_token = None
        planner.mcp_tools = SimpleNamespace(
            client=object(), workspace_path=str(DEEPDIVER_ROOT)
        )
        planner.get_session_info = lambda: {"session_id": "session-test"}
        planner._publish_agent_progress = lambda *args, **kwargs: None
        planner.execute_tool_call = lambda call: {
            "success": True, "data": {"items": []}
        }

        fake_response = SimpleNamespace(
            success=True, result={"final_article_path": "./report/final_report.md"},
            agent_name="WriterAgent", iterations=3, execution_time=1.0, error=None,
        )
        fake_writer = SimpleNamespace(execute_task=lambda task: fake_response)

        with patch.dict(os.environ, {
            "SCIA_PIPELINE_VERSION": "hybrid",
            "SKIP_LEGACY_INTERNAL_REVIEW": "true",
            "V2_MIN_VALID_REFERENCES": "0",
        }), patch("src.agents.writer_agent.create_writer_agent", return_value=fake_writer):
            result = planner.assign_subjective_task_to_writer(
                task_content="Write the evidence-grounded paper.",
                user_query="paper",
                key_files=[],
            )

        self.assertTrue(result["success"])
        self.assertTrue(planner._writer_agent_completed)

        duplicate_result = planner.assign_subjective_task_to_writer(
            task_content="Write the paper again.", user_query="paper", key_files=[]
        )
        self.assertTrue(duplicate_result["success"])
        self.assertEqual("already_completed", duplicate_result["data"]["completion_status"])

    def test_hybrid_review_gate_records_all_completed_reviewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            report = workspace / "report"
            report.mkdir()
            (report / "final_report.md").write_text("# Draft\n\nEvidence-bound text.\n", encoding="utf-8")
            reviews = [
                {"role": role, "status": "completed", "overall_score": 8,
                 "critical_issues": [], "major_issues": [], "unsupported_claims": []}
                for role in ("methodology", "experiment_evidence", "citation", "adversarial")
            ]
            with patch.dict(os.environ, {"V2_REQUIRE_CITATION_AUDIT": "false"}, clear=False), \
                 patch("src.pipeline_v2.hybrid.run_reviews", return_value=(reviews, report / "peer_review.md", [])), \
                 patch("src.pipeline_v2.hybrid.MCPTools.markdown_to_pdf", return_value=FakeResult(success=True)):
                latest, warnings = review_revision_loop(workspace, "paper", auto_revise=True)
            self.assertEqual(4, len(latest))
            self.assertFalse(any("quality gate incomplete" in warning for warning in warnings))
            self.assertTrue((report / "peer_review_rounds.json").exists())

    def test_hybrid_literature_seed_saves_structured_references_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            events = []
            online = ReferenceRecord(
                title="Verified paper", authors="A. Author", year="2025", doi="10.1000/test",
                url="https://doi.org/10.1000/test", source_type="crossref",
            )
            with patch.dict(os.environ, {"V2_MIN_VALID_REFERENCES": "1"}, clear=False), \
                 patch("src.pipeline_v2.hybrid.retrieve_online_references", return_value=([online], [])):
                count, warnings = seed_hybrid_literature(
                    workspace, "general research paper", enabled=True,
                    checkpoint=lambda stage, data: events.append((stage, data)) or [],
                )
            self.assertGreaterEqual(count, 1)
            saved = json.loads((workspace / "research" / "references.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item["title"] == "Verified paper" for item in saved))
            self.assertEqual("literature_seed_started", events[0][0])
            self.assertEqual("literature_seed_ready", events[-1][0])
            digest = json.loads((workspace / "research" / "workspace_digest.json").read_text(encoding="utf-8"))
            self.assertEqual(0, digest["summary"]["file_count"])

    def test_pubmed_identifier_mismatch_is_rejected(self):
        rejected = BaseAgent._validate_research_result(
            "get_pubmed_article", {"pmid": "12345"},
            {"success": True, "data": {"pmid": "99999", "content": "wrong paper"}},
        )
        self.assertFalse(rejected["success"])
        self.assertTrue(rejected["metadata"]["evidence_rejected"])

        accepted = BaseAgent._validate_research_result(
            "get_pubmed_article", {"pmid": "12345"},
            {"success": True, "data": {"pmid": "12345", "content": "right paper"}},
        )
        self.assertTrue(accepted["success"])

    def test_mcp_tool_result_envelope_is_flattened_before_pubmed_validation(self):
        wrapped = SimpleNamespace(
            success=True,
            data={
                "success": True,
                "data": {"pmid": "42279671", "content": "verified article"},
                "error": None,
                "metadata": {"full_text": True},
            },
            error=None,
            metadata={"transport": "mcp"},
        )
        normalized = BaseAgent._normalize_mcp_client_result(wrapped)
        accepted = BaseAgent._validate_research_result(
            "get_pubmed_article", {"pmid": "42279671"}, normalized
        )
        self.assertTrue(accepted["success"])
        self.assertEqual("42279671", accepted["data"]["pmid"])
        self.assertTrue(accepted["metadata"]["full_text"])

        failed = BaseAgent._normalize_mcp_client_result(SimpleNamespace(
            success=True,
            data={"success": False, "data": None, "error": "tool failed"},
            error=None,
            metadata={},
        ))
        self.assertFalse(failed["success"])
        self.assertEqual("tool failed", failed["error"])

    def test_tool_protocol_is_humanized_for_web_progress(self):
        raw = ('[{"name":"file_read","arguments":{"file_path":"research/workspace_digest.json"}},'
               '{"name":"file_read","arguments":{"file_path":"research/claims_evidence.json"}}]')
        readable = BaseAgent._humanize_progress_text(raw)
        self.assertIn("工作区文件概要", readable)
        self.assertIn("论点与证据关系", readable)
        self.assertNotIn("arguments", readable)
        self.assertNotIn("file_read", readable)

    def test_mojibake_guard_blocks_corrupted_user_visible_log(self):
        record = logging.LogRecord("test", logging.INFO, "planner_agent.py", 855, "瀛愪换鍔? 5 鍚姩", (), None)
        self.assertTrue(MojibakeGuardFilter().filter(record))
        rendered = record.getMessage()
        self.assertIn("已阻止乱码输出", rendered)
        self.assertNotIn("瀛愪换", rendered)

    def test_visual_communication_planner_renders_method_tables_and_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            contract = {
                "research_question": "A domain-general prediction task",
                "problem_statement": "Predict the target from multimodal observations.",
                "central_claim": "The registered method produces the task output.",
                "method_modules": ["Input encoder", "Evidence fusion", "Prediction head"],
                "datasets": ["Uploaded dataset"],
                "limitations": [],
            }
            claims = [
                {"claim_id": "C1", "claim": "Input encoder and evidence fusion are used.", "claim_type": "method", "status": "supported"},
                {"claim_id": "C2", "claim": "Metrics are reported from experiment files.", "claim_type": "result", "status": "supported"},
                {"claim_id": "C3", "claim": "Literature positioning uses registered records.", "claim_type": "literature", "status": "supported"},
            ]
            experiments = [
                {"experiment_id": "EXP1", "display_name": "baseline", "best_validation_metrics": {"accuracy": 0.8, "f1": 0.79}},
                {"experiment_id": "EXP2", "display_name": "ours+gate", "best_validation_metrics": {"accuracy": 0.86, "f1": 0.84}},
            ]
            references = [
                {"index": index, "title": f"Paper {index}", "year": "2024", "venue": "Journal", "source_type": "journal"}
                for index in range(1, 5)
            ]
            assets, warnings = plan_visual_communication(
                workspace, contract=contract, claims=claims, outline={},
                experiments=experiments, references=references,
            )
            self.assertEqual([], warnings)
            asset_ids = {asset["asset_id"] for asset in assets}
            self.assertIn("FIG_METHOD_01", asset_ids)
            self.assertIn("TAB_RELATED_01", asset_ids)
            self.assertIn("TAB_RESULTS_01", asset_ids)
            self.assertTrue((workspace / "research" / "visual_assets" / "figure_02_method_overview.svg").is_file())
            self.assertTrue((workspace / "experiment_results" / "tables" / "table_main_results.md").is_file())
            registry = json.loads((workspace / "research" / "visual_assets_registry.json").read_text(encoding="utf-8"))
            self.assertEqual("deterministic", registry["planner"])

    def test_visual_planner_does_not_force_unsupported_motivation_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets, _ = plan_visual_communication(
                Path(tmp),
                contract={
                    "problem_statement": "A research problem",
                    "central_claim": "Evidence-bound output",
                    "method_modules": ["Encoder", "Head"],
                    "limitations": ["缺少可核验的实验结果材料"],
                },
                claims=[{"claim_id": "C1", "claim": "A method exists", "claim_type": "method", "status": "supported"}],
                outline={}, experiments=[], references=[],
            )
            self.assertNotIn("FIG_MOTIVATION_01", {asset["asset_id"] for asset in assets})

    def test_visual_audit_detects_unintegrated_registered_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            assets, _ = plan_visual_communication(
                workspace,
                contract={
                    "problem_statement": "A research problem", "central_claim": "Task output",
                    "method_modules": ["Encoder", "Fusion", "Head"], "limitations": [],
                },
                claims=[{"claim_id": "C1", "claim": "method", "claim_type": "method", "status": "supported"}],
                outline={}, experiments=[], references=[],
            )
            method_asset = next(asset for asset in assets if asset["asset_id"] == "FIG_METHOD_01")
            missing = audit_manuscript_visuals(workspace, "# 方法\n\n仅有文字。")
            self.assertFalse(missing["passed"])
            relative = method_asset["files"][0]
            integrated = audit_manuscript_visuals(workspace, f"# 方法\n\n![方法总览](../{relative})\n")
            self.assertTrue(integrated["passed"])

    def test_experiment_registry_recognizes_domain_general_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "results.csv"
            csv_path.write_text(
                "epoch,validation_accuracy,f1_score,rmse,loss\n1,0.71,0.69,0.42,1.2\n2,0.86,0.84,0.25,0.7\n",
                encoding="utf-8",
            )
            analysis, _ = _read_results(csv_path)
            self.assertEqual("accuracy", analysis["primary_metric"])
            self.assertAlmostEqual(0.86, analysis["best_validation_metrics"]["accuracy"])
            self.assertAlmostEqual(0.84, analysis["best_validation_metrics"]["F1"])
            self.assertAlmostEqual(0.25, analysis["best_validation_metrics"]["RMSE"])
            self.assertNotIn("loss", analysis["best_validation_metrics"])


if __name__ == "__main__":
    unittest.main()
