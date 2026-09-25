"""Command line entry point. Module options are shared with the interactive console."""

import argparse
from typing import Any

from rich.console import Console

from fieldkit import __version__
from fieldkit.core.module import Module
from fieldkit.registry import modules


def add_options(parser: argparse.ArgumentParser, module: Module) -> None:
    parser.set_defaults(module=module)
    if module.accepts_targets:
        parser.add_argument("targets", nargs="*", help="Hostnames, IPs, or HTTP(S) origins")
    for option in module.options:
        kwargs: dict[str, Any] = {
            "dest": option.name,
            "default": option.default,
            "help": option.description,
        }
        if option.kind is bool:
            kwargs["action"] = argparse.BooleanOptionalAction
        elif option.kind is list:
            kwargs["action"] = "append"
        else:
            kwargs["type"] = option.kind
        parser.add_argument(option.flag, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fieldkit", description="Modular assessment toolkit")
    parser.add_argument("--version", action="version", version=f"Fieldkit {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("console", help="Start the interactive terminal console")
    commands.add_parser("modules", help="List installed modules")
    registry = modules()
    collect = commands.add_parser(
        "collect", help="Collect web artifacts", description=registry["web/artifacts"].description
    )
    add_options(collect, registry["web/artifacts"])
    run = commands.add_parser("run", help="Run a module directly")
    module_commands = run.add_subparsers(dest="module_name", required=True)
    for name, module in registry.items():
        subparser = module_commands.add_parser(name, help=module.description)
        add_options(subparser, module)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    if args.command in {None, "console"}:
        from fieldkit.console import InteractiveConsole

        return InteractiveConsole(console).run()
    if args.command == "modules":
        for name, module in modules().items():
            console.print(f"{name:<24} {module.description}")
        return 0
    module = args.module
    settings = {option.name: getattr(args, option.name) for option in module.options}
    if module.accepts_targets:
        settings["TARGETS"] = [*settings["TARGETS"], *args.targets]
    try:
        return module.run(settings, console)
    except (ValueError, OSError) as exc:
        console.print(f"Error: {exc}", style="red", markup=False)
        return 2
    except KeyboardInterrupt:
        return 130
