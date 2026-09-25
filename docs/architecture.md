# Architecture and module authoring

```text
src/fieldkit/
  cli.py                    argparse command routing
  console.py                interactive terminal console
  registry.py               explicit built-in module registration
  core/
    module.py               Module and Option contracts
    targets.py              target lists and origin validation
    http.py                 bounded async HTTP transport
    storage.py              private output directories and JSON reports
  modules/
    web_artifacts.py        initial collector module
tests/                      local integration and unit tests
```

Both interfaces resolve the same module objects from the registry. Each module
defines its name, description, options, and `run(settings, console)` method. Option
metadata generates the direct command flags and interactive settings table. A
module owns its validation and can reuse or replace shared services.

`run` returns an integer exit status. Validation failures should raise `ValueError`
and filesystem failures may raise `OSError`; both interfaces display these errors.
Long-running modules should handle interruption and persist useful partial output.
Keep module imports free of network, filesystem, and subprocess side effects.

## Add a module

1. Add an implementation under `src/fieldkit/modules/`. Group additional modules
   into package subdirectories when useful.
2. Subclass `Module` and declare immutable `Option` metadata.
3. Import and instantiate it in the `builtins` tuple in `registry.modules()`.
4. Add behavior tests and document the module's input and output contract.

```python
from rich.console import Console

from fieldkit.core.module import Module, Option


class Example(Module):
    name = "linux/example"
    description = "An example module contract."
    options = (Option("MESSAGE", str, "hello", "Message to display", "--message"),)

    def run(self, settings: dict, console: Console) -> int:
        console.print(settings["MESSAGE"], markup=False)
        return 0
```

After registration, `fieldkit run linux/example --message hello` and
`use linux/example` work without modifying either UI. Set `accepts_targets = True`
and define a list-valued `TARGETS` option to accept positional targets. Lists use
repeated flags in direct commands and space-separated values in the console.
Boolean options automatically support `--no-<flag>` in the CLI.

The registry is deliberately explicit: merely placing a Python file on disk does
not execute it. External plugin loading and entry-point discovery can be added
later without changing the module contract.

## Collector flow

1. Validate all options, target origins, and custom paths before making requests.
2. Create a run directory. Start a bounded pool of target workers.
3. Probe each homepage to select its scheme and obtain script references.
4. Optionally fetch a random nonexistent path as a soft-404 baseline.
5. Deduplicate built-in, custom, and discovered URLs; generate `.bak` variants.
6. Fetch candidates through a global semaphore and per-request time/size limits.
7. Save successful responses that do not match the missing-page baseline. Record
   every attempted candidate, including failures, in the manifest.
8. Write the report atomically on completion or interruption.

Targets share a connection pool and global request semaphore. Target and candidate
worker counts are bounded rather than spawning a task for every input. Responses
are streamed with a decoded-byte limit; the collector holds at most a bounded
number of candidate bodies plus per-worker homepage/baseline bodies in memory.
The report itself scales with the number of requested URLs.

Report schema version 1 uses a top-level `targets` list. Each target has `input`,
`origin`, `completed`, `had_errors`, `probes`, `notes`, and `results`; completed
candidate runs additionally have `counts`. Saved results have `file`, `bytes`,
and `sha256`. Results preserve requested and final URLs separately. Consumers
should check both run and target completion flags before interpreting counts.

## Current boundaries

The console is a command-oriented TUI, not a full-screen dashboard. There is no
background-job manager, persistent session database, authentication workflow,
recursive crawling, JavaScript runtime, secret classifier, or Linux module yet.
These can be added independently as subsequent modules or shared services.
