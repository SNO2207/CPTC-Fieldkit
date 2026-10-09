"""Protocol checks use the SDK client and a local fake assessment adapter."""

import asyncio
import importlib.util
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from fieldkit import mcp_server

HAS_MCP = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not HAS_MCP, reason="Install Fieldkit's mcp extra")


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.closed = False

    def tools(self):
        return [
            {
                "name": "fieldkit_run_example",
                "description": "Run a local fixture with explicit options.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "options": {
                            "type": "object",
                            "properties": {
                                "TARGETS": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["TARGETS"],
                            "additionalProperties": False,
                        }
                    },
                    "required": ["options"],
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": False,
                    "destructiveHint": False,
                    "idempotentHint": False,
                    "openWorldHint": True,
                },
            }
        ]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if arguments["options"]["TARGETS"] == ["raise"]:
            raise ValueError("unregistered-raw-credential-value")
        return {"job_id": "job-fixture", "status": "running", "password": "do-not-publish"}

    def list_modules(self, query=""):
        return {"modules": [{"name": "example", "description": "local fixture"}]}

    def list_jobs(self, limit=20):
        return {"jobs": [{"job_id": "job-fixture", "status": "running"}]}

    def read_report(self, job_id, section="summary", offset=0, limit=50):
        if job_id != "job-fixture":
            raise ValueError("private-data-in-error")
        return {"job_id": job_id, "section": section, "password": "do-not-publish"}

    async def close(self):
        self.closed = True


@requires_mcp
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_discovers_tools_calls_resources_and_prompt(mode):
    from mcp import Client

    agent = FakeAgent()

    async def exercise():
        async with Client(mcp_server.build_server(agent), mode=mode) as client:
            tools = await client.list_tools()
            assert tools.tools[0].name == "fieldkit_run_example"
            assert tools.tools[0].annotations.open_world_hint is True
            assert tools.tools[0].input_schema == agent.tools()[0]["input_schema"]
            result = await client.call_tool(
                "fieldkit_run_example", {"options": {"TARGETS": ["http://fixture.invalid/"]}}
            )
            assert not result.is_error
            assert result.structured_content["job_id"] == "job-fixture"
            assert result.structured_content["password"] == "[REDACTED]"
            assert "do-not-publish" not in result.model_dump_json()
            resources = await client.list_resources()
            assert {str(item.uri) for item in resources.resources} == {
                "fieldkit://modules",
                "fieldkit://jobs",
            }
            templates = await client.list_resource_templates()
            assert templates.resource_templates[0].uri_template == "fieldkit://jobs/{job_id}/report"
            catalog = await client.read_resource("fieldkit://modules")
            assert json.loads(catalog.contents[0].text)["modules"][0]["name"] == "example"
            jobs = await client.read_resource("fieldkit://jobs")
            assert json.loads(jobs.contents[0].text)["jobs"][0]["job_id"] == "job-fixture"
            report = await client.read_resource("fieldkit://jobs/job-fixture/report")
            assert json.loads(report.contents[0].text)["password"] == "[REDACTED]"
            prompts = await client.list_prompts()
            assert prompts.prompts[0].name == "fieldkit_assessment_workflow"
            prompt = await client.get_prompt("fieldkit_assessment_workflow")
            assert "untrusted data" in prompt.messages[0].content.text
            assert "DRY_RUN" in prompt.messages[0].content.text

    asyncio.run(exercise())
    assert len(agent.calls) == 1


@requires_mcp
def test_real_assessment_tools_are_discoverable_and_run_private_offline_jobs(tmp_path):
    from mcp import Client

    from fieldkit.core.agent import FieldkitAgent
    from fieldkit.core.jwt import encode

    token = encode(b'{"alg":"none"}') + "." + encode(b'{"sub":"fixture-private-subject"}') + "."
    token_file = tmp_path / "private-token.txt"
    token_file.write_text(token)
    token_file.chmod(0o600)
    requests = tmp_path / "private-requests.json"
    requests.write_text(
        json.dumps(
            {
                "requests": [
                    {
                        "id": "preview",
                        "url": "https://fixture.test/preview",
                        "method": "POST",
                        "json": {"url": "https://fixture.test/echo"},
                    }
                ]
            }
        )
    )
    requests.chmod(0o600)
    imported = tmp_path / "ffuf.json"
    imported.write_text(
        json.dumps({"results": [{"url": "https://fixture.test/preview", "status": 200}]})
    )

    async def exercise():
        agent = FieldkitAgent(tmp_path)
        try:
            async with Client(mcp_server.build_server(agent)) as client:
                catalog = await client.list_tools()
                names = {tool.name for tool in catalog.tools}
                assert len(names) == 53
                assert {
                    "fieldkit_run_web_parameter_discovery",
                    "fieldkit_run_web_jwt_check",
                    "fieldkit_run_web_ffuf",
                    "fieldkit_run_integrations_burp_bridge",
                } <= names
                cases = [
                    ("fieldkit_run_web_jwt_check", {"TOKEN_FILE": token_file.name}),
                    (
                        "fieldkit_run_web_parameter_discovery",
                        {
                            "TARGETS": ["https://fixture.test"],
                            "REQUESTS_FILE": requests.name,
                            "REQUEST_IDS": ["preview"],
                            "NAMES": ["method", "body", "bearer"],
                            "DRY_RUN": True,
                        },
                    ),
                    (
                        "fieldkit_run_web_ffuf",
                        {"INPUT": imported.name, "IMPORT_COMPLETE": True},
                    ),
                ]
                for tool, options in cases:
                    result = await client.call_tool(tool, {"options": options, "wait_seconds": 10})
                    assert not result.is_error, result.model_dump_json()
                    status = result.structured_content
                    assert status["status"] == "completed", status
                    report = await client.call_tool(
                        "fieldkit_read_report", {"job_id": status["job_id"], "section": "results"}
                    )
                    assert report.structured_content["available"]
                    assert token not in report.model_dump_json()
                    assert "fixture-private-subject" not in report.model_dump_json()
        finally:
            await agent.close()

    asyncio.run(exercise())


@requires_mcp
@pytest.mark.parametrize(
    "arguments,constraint",
    [
        ({}, "required"),
        ({"options": {"TARGETS": "private-secret-value"}}, "type"),
        ({"options": {"TARGETS": [], "PASSWORD": "private-secret-value"}}, "additionalProperties"),
    ],
)
def test_schema_rejection_is_actionable_and_does_not_echo_values(arguments, constraint):
    from mcp import Client

    agent = FakeAgent()

    async def exercise():
        async with Client(mcp_server.build_server(agent)) as client:
            result = await client.call_tool("fieldkit_run_example", arguments)
            assert result.is_error
            assert result.structured_content["constraint"] == constraint
            assert "private-secret-value" not in result.model_dump_json()

    asyncio.run(exercise())
    assert not agent.calls


@requires_mcp
def test_unknown_tool_and_unexpected_exception_are_public_errors():
    from mcp import Client

    async def exercise():
        async with Client(mcp_server.build_server(FakeAgent()), mode="legacy") as client:
            unknown = await client.call_tool("unknown", {})
            assert unknown.is_error
            assert unknown.structured_content["error"] == "Unknown Fieldkit tool."
            failed = await client.call_tool(
                "fieldkit_run_example", {"options": {"TARGETS": ["raise"]}}
            )
            assert failed.is_error
            assert "unregistered-raw-credential-value" not in failed.model_dump_json()
            missing = await client.read_resource("fieldkit://jobs/missing/report")
            assert json.loads(missing.contents[0].text)["error"] == "Resource is unavailable."
            assert "private-data-in-error" not in missing.model_dump_json()
            private = await client.read_resource("file:///etc/passwd")
            assert json.loads(private.contents[0].text)["error"] == "Unknown Fieldkit resource."

    asyncio.run(exercise())


@requires_mcp
def test_explicit_public_agent_errors_are_actionable():
    from mcp import Client

    from fieldkit.core.agent import FieldkitAgentError

    class LimitedAgent(FakeAgent):
        async def call(self, name, arguments):
            raise FieldkitAgentError("Active job limit reached; wait for or cancel a job")

    async def exercise():
        async with Client(mcp_server.build_server(LimitedAgent())) as client:
            result = await client.call_tool("fieldkit_run_example", {"options": {"TARGETS": []}})
            assert result.is_error
            assert "Active job limit reached" in result.structured_content["error"]

    asyncio.run(exercise())


@requires_mcp
def test_invalid_server_options_are_actionable_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert mcp_server.main(["--max-jobs", "0"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "MAX_JOBS must be 1-8" in captured.err
    assert "Traceback" not in captured.err


@requires_mcp
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_subprocess_stdio_and_eof_cleanup(tmp_path, mode):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    marker = tmp_path / "closed"
    script = tmp_path / "fixture_server.py"
    test_directory = Path(__file__).resolve().parent
    script.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from fieldkit.mcp_server import serve\n"
        "from test_mcp_protocol import FakeAgent\n"
        "class Agent(FakeAgent):\n"
        "    async def call(self, name, arguments):\n"
        "        print('fixture stray handler stdout')\n"
        "        return await super().call(name, arguments)\n"
        "    async def close(self):\n"
        f"        Path({str(marker)!r}).write_text('closed')\n"
        "asyncio.run(serve(Agent()))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(test_directory), str(Path.cwd() / "src")])
    params = StdioServerParameters(command=sys.executable, args=[str(script)], env=env)

    async def exercise():
        async with Client(params, mode=mode) as client:
            listed = await client.list_tools()
            assert listed.tools[0].name == "fieldkit_run_example"
            response = await client.call_tool("fieldkit_run_example", {"options": {"TARGETS": []}})
            assert response.structured_content["status"] == "running"

    asyncio.run(exercise())
    assert marker.read_text() == "closed"


def test_missing_optional_sdk_is_actionable_and_keeps_stdout_empty(monkeypatch, capsys):
    def unavailable():
        raise ImportError("fixture missing package")

    monkeypatch.setattr(mcp_server, "_load_sdk", unavailable)
    assert mcp_server.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "pip install" in captured.err
    assert "Traceback" not in captured.err


def test_import_does_not_load_optional_sdk():
    import subprocess

    source = """import sys
class BlockSDK:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mcp' or fullname.startswith('mcp.') or fullname == 'jsonschema':
            raise ImportError('SDK unavailable')
sys.meta_path.insert(0, BlockSDK())
import fieldkit.mcp_server
print('imported')
"""
    run = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=True)
    assert run.stdout == "imported\n"
    assert not run.stderr


def test_server_releases_jobs_when_connection_is_cancelled(monkeypatch):
    agent = FakeAgent()
    entered = asyncio.Event()

    class Server:
        def create_initialization_options(self):
            return None

        async def run(self, read_stream, write_stream, options):
            entered.set()
            await asyncio.Event().wait()

    @asynccontextmanager
    async def transport():
        yield None, None

    monkeypatch.setattr(mcp_server, "_load_sdk", lambda: (Server, None, transport, None))
    monkeypatch.setattr(mcp_server, "build_server", lambda agent: Server())

    async def exercise():
        task = asyncio.create_task(mcp_server.serve(agent))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert agent.closed


@requires_mcp
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_cli_stdio_registry_and_dry_run_job(tmp_path, mode):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    from fieldkit.registry import modules

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fieldkit", "mcp", "--workspace", str(tmp_path)],
    )

    async def exercise():
        async with Client(server, mode=mode) as client:
            listed = await client.list_tools()
            expected = {
                "fieldkit_run_" + name.replace("/", "_").replace("-", "_") for name in modules()
            }
            assert {
                item.name for item in listed.tools if item.name.startswith("fieldkit_run_")
            } == (expected)
            info = await client.call_tool("fieldkit_module_info", {"module": "web/header-review"})
            assert not info.is_error, info.structured_content
            properties = info.structured_content["input_schema"]["properties"]["options"]
            assert "OUTPUT" not in properties["properties"]
            run = await client.call_tool(
                "fieldkit_run_web_header_review",
                {
                    "options": {"TARGETS": ["http://127.0.0.1:9/"], "DRY_RUN": True},
                    "wait_seconds": 5,
                },
            )
            assert not run.is_error, run.structured_content
            job_id = run.structured_content["job_id"]
            state = await client.call_tool("fieldkit_job_status", {"job_id": job_id})
            assert state.structured_content["status"] == "completed", state.structured_content
            report = await client.call_tool("fieldkit_read_report", {"job_id": job_id})
            assert report.structured_content["summary"]["completed"] is True
            assert report.structured_content["summary"]["counts"]["evidence"] == 0
            report_resource = await client.read_resource(f"fieldkit://jobs/{job_id}/report")
            assert json.loads(report_resource.contents[0].text)["summary"]["completed"] is True
            runs = await client.call_tool("fieldkit_list_jobs", {})
            assert runs.structured_content["jobs"][0]["job_id"] == job_id
            return job_id

    job_id = asyncio.run(exercise())
    job_directory = tmp_path / "artifacts" / "mcp" / job_id
    assert not (job_directory / ".settings.json").exists()
    report = json.loads(next((job_directory / "runs").glob("*/report.json")).read_text())
    assert report["plan"]["validated_settings"]["DRY_RUN"] is True
    assert report["plan"]["network_requests"] == 0
