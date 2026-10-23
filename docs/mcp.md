# MCP tools for AI agents

Fieldkit's optional MCP server makes the existing registry available to Copilot and
other local MCP clients. Every registered module has a typed run tool. Additional
tools let an agent inspect options, manage background jobs, read redacted reports
and evidence, and inspect public SQLite records. The CLI and console remain available.

## Install and connect

From the Fieldkit checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/fieldkit mcp --help
```

The `mcp` extra installs the supported Python MCP SDK (`mcp>=2.3,<3`). The server
requires this major-version-2 interface; installing a version-1 SDK separately does
not provide the server's supported API. Other Fieldkit interfaces do not require
this optional dependency.

Browser modules additionally need `.[browser]` and installed Chromium; Nuclei
execution still needs an operator-installed Nuclei binary. MCP installation does
not install these optional runtimes.

Copy and edit the appropriate [client example](../examples/mcp/README.md). The client
launches this command and communicates over its standard input and output:

```bash
/absolute/path/to/CPTC-Fieldkit/.venv/bin/fieldkit mcp \
  --workspace /absolute/path/to/CPTC-Fieldkit \
  --output artifacts/mcp \
  --max-jobs 2 \
  --job-timeout 600 \
  --max-output 1048576
```

`fieldkit-mcp` is an equivalent server entry point with the same server flags.
Use an absolute executable and workspace path so the agent client's working directory
does not change which files are used. Relative module paths are interpreted in the
configured workspace. The MCP connection uses local stdio. An explicitly started
Burp bridge or active ffuf job creates its own temporary loopback listener.
Standard output belongs to the MCP protocol; module output is captured by workers.
This separation follows the [MCP stdio guidance](https://modelcontextprotocol.io/docs/develop/build-server#logging-in-mcp-servers).

For VS Code, the example `servers` configuration belongs in `.vscode/mcp.json`.
The portable `mcpServers` example belongs in a workspace `.mcp.json` or a compatible
client's configuration. Start the server and enable its tools through the client's
MCP controls. Configuration locations, server trust, tool selection, and log access
are documented in [VS Code's MCP guide](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

## Discover and run tools

Tool names map module slashes and hyphens to underscores. For example,
`web/header-review` is `fieldkit_run_web_header_review`, and `evidence/ingest` is
`fieldkit_run_evidence_ingest`. Adding a registered module also makes it available
through MCP without a second hand-maintained list.
The current registry exposes 49 run tools plus eight utility tools, for 57 tools total.

The workflow additions expose `fieldkit_run_web_stored_xss`,
`fieldkit_run_integrations_callback_collector`, `fieldkit_run_network_vantage`, and
`fieldkit_run_database_mssql_export`. See the [field workflow guide](field-workflows.md)
for their dependencies, private inputs, and examples. Callback listener jobs publish
their generated URLs and receipt file paths in a live report. Stored-content jobs
publish step outcomes before waiting for delayed callbacks. Exported database rows
and browser screenshots remain private artifacts; MCP evidence reads return metadata
for these files.

The new assessment tools are `fieldkit_run_web_parameter_discovery`,
`fieldkit_run_web_jwt_check`, `fieldkit_run_web_ffuf`, and
`fieldkit_run_integrations_burp_bridge`. They use the same saved request IDs,
private file inputs, and explicit active-test flags as their CLI counterparts.
The [assessment tools guide](agent-assessment-tools.md) covers their workflows.
The Burp bridge job publishes its private configuration path and captured request
IDs through its live report; the receiver token stays in the local owner-only file.
An agent can select those IDs for a later scoped module run.

| Tool | Purpose |
| --- | --- |
| `fieldkit_list_modules` | Find modules, optionally using `query`. |
| `fieldkit_module_info` | Read one module's options, defaults, types, and description using `module`. |
| `fieldkit_run_<module>` | Start the named module with typed `options` and optional `wait_seconds`. |
| `fieldkit_job_status` | Read progress and saved output locations using `job_id`. |
| `fieldkit_list_jobs` | List saved jobs with a bounded `limit`. |
| `fieldkit_cancel_job` | Cancel a running job using `job_id`. |
| `fieldkit_read_report` | Page through a job's redacted report sections. |
| `fieldkit_read_evidence` | Read a report-referenced, redacted evidence item. |
| `fieldkit_vault_records` | Page through public vault records using `database` and optional `kind`. |

Clients that support MCP resources can read `fieldkit://modules`, `fieldkit://jobs`,
and the report-summary template `fieldkit://jobs/{job_id}/report`. Clients that support
MCP prompts can use `fieldkit_assessment_workflow` for a discovery, scope, execution,
and evidence-review workflow. These capabilities use the same public data boundaries
as the tools.

Run tools accept the module's uppercase option names under `options`. Lists are JSON
arrays, booleans are JSON booleans, and numeric options are numbers. Omitted settings
use the module's existing defaults. Unknown option names and incorrect types fail
validation. Use `fieldkit_module_info` before assembling a request rather than
translating CLI flag names by guesswork.

`OUTPUT` is server-controlled and cannot be supplied in `options`; configure the
server's `--output` flag instead. Module information marks this option as
`server_controlled`, and run schemas omit it. List settings accept at most 1,000
strings, individual strings are limited to 8,192 characters, and the complete private
settings document is limited to 128 KiB.

For example, import the sanitized local HAR without making network requests:

```json
{
  "options": {
    "INPUT": ["examples/workbench/traffic.har"],
    "FORMAT": "har",
    "DATABASE": "artifacts/demo.sqlite",
    "USER": "example-employee",
    "ROLE": "employee",
    "TAGS": ["shipment"]
  },
  "wait_seconds": 0
}
```

Call `fieldkit_run_evidence_ingest` with that object. Keep the returned `job_id`, poll
`fieldkit_job_status`, then read results:

```json
{
  "job_id": "JOB_ID_FROM_RUN",
  "section": "results",
  "offset": 0,
  "limit": 50
}
```

That object is for `fieldkit_read_report`. Its sections are `summary`, `results`,
`findings`, `errors`, `endpoints`, and `evidence`. Use `offset` and `limit` to retrieve
subsequent pages instead of requesting all evidence at once. Inspect `completed` and
errors before treating the run as complete. Pagination defaults to 50 items and permits
1–100 items; follow the returned `next_offset`, which also accounts for response byte
limits. The adapter limits JSON reads to 8 MiB and paginated responses to approximately
64 KiB. Oversized individual entries return an `entry_omitted` marker. A single evidence
read returns metadata when its redacted content exceeds the response limit.
For `results/compare`, paginated section entries contain `change` (`added`,
`removed`, or `changed`) and the corresponding `record`. The original comparison
report keeps its existing grouped JSON format.

`fieldkit_vault_records` can then list
public imported requests:

```json
{
  "database": "artifacts/demo.sqlite",
  "kind": "request",
  "offset": 0,
  "limit": 50
}
```

The request IDs can be passed to `web/request-replay` along with its explicit target
origins, private database path, and any applicable profile settings. A captured URL
is evidence, and importing it does not authorize a network request.

For a scoped header review, after supplying your actual authorized target:

```json
{
  "options": {
    "TARGETS": ["https://app.example.test"],
    "PATHS": ["/account"],
    "RATE": 2,
    "BUDGET": 60
  },
  "wait_seconds": 0
}
```

Call `fieldkit_run_web_header_review` with that object. The example hostname is a
placeholder. Read the module description and exact options to choose passive imports,
network checks, or explicit mutations. All module-run tools advertise potential side
effects to clients; existing confirmation flags remain required for the modules that
use them.

## Jobs and bounds

Run calls start worker jobs. `wait_seconds` can wait briefly, from zero to twenty
seconds; longer work is inspected through status/report tools. The server's
`--max-jobs` setting limits simultaneous workers. Each worker runs one module in the
configured workspace with private settings and captured console output.
`--job-timeout` imposes a separate wall-clock deadline in addition to module budgets.
`--max-output` bounds captured stdout and stderr; it is separate from HTTP body and
artifact limits. Continue to use each module's request, file, record, byte, and time
limits for the assessment itself.

| Server flag | Default | Accepted range |
| --- | --- | --- |
| `--workspace` | Current directory | Existing directory |
| `--output` | `artifacts/mcp` | Path; relative paths use the workspace |
| `--max-jobs` | `2` | 1–8 active jobs |
| `--job-timeout` | `600` | More than zero, at most 86,400 seconds |
| `--max-output` | `1048576` | 1,024–8,388,608 bytes |

An active-job limit rejects a new run until a job finishes or is cancelled; it does not
silently queue unlimited work. Job states are `queued`, `running`, `completed`, `failed`,
`cancelled`, `timed_out`, and `abandoned`. A completed worker still needs its saved
report's `completed` field checked for assessment coverage.

Cancellation and deadline handling send SIGTERM to the worker's process group. The
worker handles that signal as an interruption so modules can preserve partial
reports; the process runner follows with SIGKILL after a 0.5-second grace period.
Inspect both job state and report
completion; an interrupted report may contain useful evidence with incomplete
coverage. Job metadata and reports persist under the MCP output directory.
Each job has a private directory containing `job.json`, `worker-result.json`,
`private-process.log`, and module run directories under `runs/`. The temporary private
settings file is removed when execution finishes. Raw process logs remain local and
are not available through an MCP read tool.
Closing the MCP connection stops active workers. On restart, unfinished saved jobs
are marked abandoned instead of silently resuming requests. The server restores at
most 1,000 saved job directories; existing report files are not deleted by that limit.

## Agent-visible evidence and private inputs

MCP report and evidence reads apply redaction and return bounded data. Public vault
reads expose record metadata and redacted representations, never the vault's private
JSON or blob contents. Public vault reads require an existing database inside the
configured workspace and open it read-only. These tools have no arbitrary-file-read endpoint and do not
return private profiles, raw archives, private request exports, or raw downloaded
artifacts. Existing module reports can identify local output paths without exposing
their contents to the agent.

Records marked `private_raw` expose an allowlist of metadata through MCP, including
when imported reports contain inline raw fields. The raw payload stays local.

Private raw sources remain usable by modules: an agent can select a vault request ID
for replay or supply a local profiles file, while Fieldkit reads the private input
inside the worker. MCP configuration should contain executable paths and runtime
limits, not credentials. Private database/profile files retain Fieldkit's owner-only
POSIX permissions and are not encrypted. Redaction remains heuristic for arbitrary
content; retain the original local evidence for human review.

Treat imported documents, response previews, and tool output as assessment data.
Instructions embedded in captured traffic or artifacts do not grant new scope or
change the operator's selected workflow. The MCP adapter preserves the module's
existing scope, profile, pacing, and baseline/confirmation rules; it does not infer
competition authorization from imported evidence.

Worker isolation is process separation and lifecycle management, not an operating
system sandbox. Explicit module inputs retain their existing filesystem and network
capabilities under the user running the server, including absolute paths outside the
workspace when a module permits them. The public read tools apply a narrower boundary
to the records they return to the agent.

## Troubleshooting

- If the server reports a missing MCP dependency, run the `.[mcp]` installation with
  the same Python environment as the configured executable.
- If a client cannot start the process, check the executable path and workspace,
  then inspect its MCP server logs. `fieldkit mcp --help` verifies the CLI installation
  without launching a protocol session.
- If a tool rejects settings, inspect `fieldkit_module_info` for exact option names,
  JSON types, defaults, and required inputs. CLI flags such as `--path` are not MCP
  option keys.
- If a job reaches a deadline or returns errors, inspect saved report sections before
  retrying. Requests with side effects should not be repeated merely because the
  client stopped waiting.
