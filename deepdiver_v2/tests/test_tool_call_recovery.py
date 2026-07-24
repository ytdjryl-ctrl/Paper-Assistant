import logging

from src.agents.base_agent import BaseAgent
from src.agents.experiment_agent import ExperimentAgent


class _TestAgent(BaseAgent):
    def _build_system_prompt(self):
        return ""

    def execute_task(self, task_input):
        raise NotImplementedError


def _bare_agent():
    agent = _TestAgent.__new__(_TestAgent)
    agent.logger = logging.getLogger("test-tool-call-recovery")
    agent.tool_schemas = [
        {
            "type": "function",
            "function": {
                "name": "list_workspace",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "recursive": {"type": "boolean"},
                        "max_depth": {"type": "integer"},
                    },
                    "required": ["path"],
                },
            },
        }
    ]
    return agent


def test_first_complete_json_is_kept_when_provider_appends_another_block():
    agent = _bare_agent()
    content = (
        '[unused11][{"name":"list_workspace","arguments":'
        '{"path":"research","recursive":true,"max_depth":3}}]'
        '[{"name":"think","arguments":{"thought":"already have enough results"}}]'
        '[unused12]'
    )

    calls = agent.extract_tool_calls(content)

    assert calls == [{
        "name": "list_workspace",
        "arguments": {"path": "research", "recursive": True, "max_depth": 3},
    }]


def test_mismatched_recovered_arguments_are_rejected_instead_of_executed():
    agent = _bare_agent()
    content = (
        '[unused11][{"name":"list_workspace","arguments":{"path":"research"}} BROKEN '
        '{"thought":"this belongs to a later reasoning block"}[unused12]'
    )

    calls = agent.extract_tool_calls(content)

    assert calls[0]["name"] == "system_error_feedback"
    assert "Invalid arguments for list_workspace" in calls[0]["arguments"]["error"]


def test_tool_call_signature_is_stable_for_argument_order():
    first = BaseAgent.tool_call_signature("academic_search", {"queries": ["a"], "limit": 10})
    second = BaseAgent.tool_call_signature("academic_search", {"limit": 10, "queries": ["a"]})

    assert first == second


def test_search_argument_alias_is_normalized_before_schema_validation():
    agent = _bare_agent()
    agent.tool_schemas = [{
        "type": "function",
        "function": {
            "name": "search_pubmed_key_words",
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["keywords"],
            },
        },
    }]

    calls = agent.extract_tool_calls(
        '[{"name":"search_pubmed_key_words","arguments":{"query":"apple ripeness","max_results":10}}]'
    )

    assert calls == [{
        "name": "search_pubmed_key_words",
        "arguments": {"keywords": "apple ripeness", "max_results": 10},
    }]


def test_plural_queries_are_split_for_singular_search_tool():
    agent = _bare_agent()
    agent.tool_schemas = [{
        "type": "function",
        "function": {
            "name": "arxiv_search",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
                "required": ["query"],
            },
        },
    }]

    calls = agent.extract_tool_calls(
        '[{"name":"arxiv_search","arguments":{"queries":["GhostNet","cross attention"],"max_results":5}}]'
    )

    assert [call["arguments"]["query"] for call in calls] == ["GhostNet", "cross attention"]


def test_experiment_shell_source_generation_is_blocked():
    assert ExperimentAgent._bash_attempts_source_write(
        'python -c "import base64; open(\'experiment_results/run.py\', \'w\').write(\'x\')"'
    )
    assert ExperimentAgent._bash_attempts_source_write(
        "cat > experiment_results/run.py << 'PY'\nprint('x')\nPY"
    )
    assert not ExperimentAgent._bash_attempts_source_write("dir experiment_results")


def test_experiment_directory_signatures_normalize_windows_paths():
    first = ExperimentAgent._experiment_tool_signature(
        "bash", {"command": "dir ./experiment_results"}
    )
    second = ExperimentAgent._experiment_tool_signature(
        "bash", {"command": "DIR experiment_results\\"}
    )
    assert first == second
