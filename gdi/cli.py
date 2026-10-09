"""Git exchange, autonomous CI clients, and persistent host worker."""

import argparse
import json
import logging
import sys

from . import __version__
from .banner import render as render_banner
from .exchange import CHECKPOINT_EVERY, Exchange
from .git import GdiError, Git


class VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        print("gdi " + __version__)
        print("\n" + render_banner())
        parser.exit(0)


def workflow_arguments(command):
    command.add_argument("--workflow", help="workflow YAML file or directory (default: .github/workflows)")
    command.add_argument("--event", help="workflow event (default: push)")
    command.add_argument("--job", help="optional workflow job ID")
    command.add_argument("--input", action="append", default=[], metavar="KEY=VALUE", help="workflow input; repeatable")


def selected_workflow(args):
    from .ci_protocol import workflow_selection
    inputs = {}
    for value in args.input:
        key, separator, content = value.partition("=")
        if not separator or not key or key in inputs:
            raise GdiError("workflow inputs must be unique KEY=VALUE arguments")
        inputs[key] = content
    return workflow_selection({"path": args.workflow if args.workflow is not None else ".github/workflows",
                               "event": args.event if args.event is not None else "push",
                               "job": args.job if args.job is not None else "", "inputs": inputs})


def parser():
    cli = argparse.ArgumentParser(
        prog="gdi", description="Verified Git bundle exchange through rclone.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Quick start (run exchange commands inside a Git worktree):
  gdi remote add drive gdrive:gdi/my-project --init   # empty Drive folder, once
  gdi push drive                                      # publish committed branch HEAD
  gdi remote add drive gdrive:gdi/my-project          # join from another clone
  gdi fetch drive                                     # import without changing files
  gdi pull drive                                      # clean worktree, fast-forward
  gdi cache clear drive                               # clear local verified cache
  gdi push drive --ci --worker user-host --profile full
  gdi ci wait drive JOB_ID --follow                    # watch CI and console
  gdi gc drive                                        # preview remote cleanup
  gdi gc drive --apply --quiescent                    # all clients must be paused

Setup: INSTALL.md. Human reference: QUICKSTART.md.
Use gdi COMMAND --help for command options; gdi -v/--version shows version and emblem.""")
    cli.epilog = "\n".join(line.split("#", 1)[0].rstrip().ljust(55) + "# " + line.split("#", 1)[1].strip()
                                 if line.startswith("  gdi ") and "#" in line else line
                                 for line in cli.epilog.split("\n"))
    cli.add_argument("-v", "--version", action=VersionAction, nargs=0, help="show version and the GDI emblem")
    commands = cli.add_subparsers(dest="command", required=True)
    remote = commands.add_parser("remote", help="configure a dedicated rclone folder for this repository")
    operations = remote.add_subparsers(dest="operation", required=True)
    add = operations.add_parser("add", help="join an existing remote, or initialize an empty folder")
    add.add_argument("name")
    add.add_argument("url", help="e.g. gdrive:gdi/my-project")
    add.add_argument("--init", action="store_true", help="initialize a new empty remote folder once")
    add.add_argument("--inbox-root", help="shared exchange root (default: parent of repository URL)")
    add.add_argument("--repository-id", help="require this known identity when joining an existing remote")
    operations.add_parser("list", help="list local gdi remotes")
    remove = operations.add_parser("remove", help="remove local config; retain remote files and fetched refs")
    remove.add_argument("name")
    cache = commands.add_parser("cache", help="manage the disposable local verified-object cache")
    cache_operations = cache.add_subparsers(dest="operation", required=True)
    clear = cache_operations.add_parser("clear", help="clear a remote's local cache without changing Git refs or remote files")
    clear.add_argument("remote")
    gc = commands.add_parser("gc", help="plan removal of obsolete remote bundles (dry run by default)")
    gc.add_argument("remote", help="local gdi remote name; inspect every published branch")
    gc.add_argument("--keep-checkpoints", type=int, default=2,
                    help="keep this many latest full checkpoints and following bundles per branch (default: 2)")
    gc.add_argument("--apply", action="store_true", help="verify retained history from scratch, then delete planned bundles")
    gc.add_argument("--quiescent", action="store_true",
                    help="confirm ALL clients have paused push/fetch/pull/gc; required with --apply, not a distributed lock")
    for name in ("push", "fetch", "pull"):
        command = commands.add_parser(name, help={"push": "publish committed branch history",
            "fetch": "verify and update a remote-tracking ref", "pull": "fetch current branch and fast-forward only"}[name])
        command.add_argument("remote", help="local gdi remote name")
        if name != "pull":
            command.add_argument("branch", nargs="?", help="branch name (default: current branch)")
        if name == "push":
            command.add_argument("--full", action="store_true", help="publish a self-contained checkpoint")
            command.add_argument("--checkpoint-every", type=int, default=CHECKPOINT_EVERY,
                                 help=f"publish a full checkpoint every N updates (default: {CHECKPOINT_EVERY})")
            command.add_argument("--ci", action="store_true", help="submit exact published commit to the host worker")
            command.add_argument("--worker", help="registered worker ID (required with --ci)")
            command.add_argument("--profile", default="full", help="CI execution profile (default: full)")
            workflow_arguments(command)
            command.add_argument("--json", action="store_true", help="write one machine-readable response")
        if name == "pull":
            command.add_argument("--passed", action="store_true", help="apply exactly the selected verified PASS commit")
            command.add_argument("--job", help="explicit CI job ID; required with --passed")
            command.add_argument("--profile", help="required CI profile; required with --passed")
    ci = commands.add_parser("ci", help="submit jobs, inspect progress and retrieve verified CI results")
    ci_ops = ci.add_subparsers(dest="operation", required=True)
    for operation in ("submit", "status", "wait", "logs", "retry"):
        sub = ci_ops.add_parser(operation)
        sub.add_argument("remote")
        if operation == "submit":
            sub.add_argument("--publication", required=True)
            sub.add_argument("--worker", required=True)
            sub.add_argument("--profile", default="full")
            workflow_arguments(sub)
        else:
            sub.add_argument("job")
        if operation in ("wait", "logs"):
            sub.add_argument("--follow", action="store_true")
        if operation == "wait":
            sub.add_argument("--timeout", type=float, default=3600)
        if operation == "logs":
            sub.add_argument("--output", help="save the verified complete binary log after completion")
        else:
            sub.add_argument("--json", action="store_true")
    worker = commands.add_parser("worker", help="host worker and systemd user service (works outside Git)")
    worker_ops = worker.add_subparsers(dest="operation", required=True)
    for operation in ("check", "run", "install", "start", "status", "stop"):
        sub = worker_ops.add_parser(operation)
        if operation in ("check", "run", "install"):
            sub.add_argument("--config", default="~/.config/gdi/worker.json")
        if operation == "run":
            sub.add_argument("--once", action="store_true", help="poll once and finish discovered jobs, then exit")
        if operation in ("status", "check"):
            sub.add_argument("--json", action="store_true")
        if operation == "check":
            sub.add_argument("--runtime", action="store_true", help="verify actual act/Docker/images and show execution revision")
    return cli


def emit(value, machine=False):
    if machine:
        print(json.dumps(value, sort_keys=True))
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def main(argv=None):
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command == "push" and (args.ci and (not args.worker or not args.profile) or
                                  not args.ci and (args.worker or args.profile != "full" or args.workflow or args.event or args.job or args.input)):
        command_parser.error("--ci requires --worker; CI options require --ci (profile defaults to full)")
    if args.command == "pull" and (args.passed and (not args.job or not args.profile) or
                                  not args.passed and (args.job or args.profile)):
        command_parser.error("--passed requires --job and --profile; these options are used together")
    if args.command == "ci" and args.operation == "logs" and args.output and args.follow:
        command_parser.error("choose logs --output or --follow")
    try:
        selector = None
        if args.command == "push" and args.ci or args.command == "ci" and args.operation == "submit":
            from .ci_protocol import identifier
            identifier(args.worker)
            identifier(args.profile)
            selector = selected_workflow(args)
        if args.command == "worker":
            from . import service
            from .worker_config import load_config
            if args.operation in ("check", "run", "install"):
                config = load_config(args.config)
                if args.operation == "check":
                    if args.runtime and config['config_version'] == 2:
                        from .runner import resolve_profile
                        import tempfile
                        with tempfile.TemporaryDirectory(prefix='gdi-check-') as cwd:
                            config['execution_profile'] = resolve_profile(config['execution_profile'], cwd)
                    details = ({"remote_url": config["remote_url"], "inbox": "inbox",
                                ("execution_revision" if args.runtime else "config_revision"): config["execution_profile"]["revision"],
                                **({"environment": config['execution_profile']['environment']} if args.runtime else {})}
                               if config["config_version"] == 2 else {"repositories": {
                                   repo["repository_id"]: {name: profile["revision"] for name, profile in repo["profiles"].items()}
                                   for repo in config["repositories"]}})
                    if args.runtime and config['config_version'] != 2:
                        raise GdiError('--runtime requires global worker config version 2')
                    emit({"worker_id": config["worker_id"], **details, "valid": True}, args.json)
                elif args.operation == "install":
                    print("Installed " + str(service.install(args.config)) + "; run gdi worker start")
                else:
                    from .worker import Worker
                    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
                    worker = Worker(config)
                    try:
                        worker.run(once=args.once)
                    finally:
                        worker.close()
            else:
                emit(service.action(args.operation), getattr(args, "json", False))
            return 0
        git = Git.discover()
        exchange = Exchange(git)
        if args.command == "ci":
            from .ci import CiClient
            client = CiClient(exchange, args.remote)
            if args.operation in ("submit", "retry"):
                with git.lock():
                    value = (client.submit(args.publication, args.worker, args.profile, workflow=selector) if args.operation == "submit"
                             else client.retry(args.job))
            elif args.operation == "status":
                value = client.status(args.job)
            elif args.operation == "wait":
                value, code = client.wait(args.job, timeout=args.timeout, follow=args.follow)
                emit(value, args.json)
                return code
            else:
                client.logs(args.job, output=args.output, follow=args.follow)
                return 0
            emit(value, args.json)
            return 0
        with git.lock():
            if args.command == "remote":
                if args.operation == "add":
                    identity = exchange.add(args.name, args.url, initialize=args.init, expected_id=args.repository_id, inbox_root=args.inbox_root)
                    print(f"Remote {args.name}: {args.url}\nRepository ID: {identity}")
                elif args.operation == "remove":
                    exchange.remove(args.name)
                    print(f"Removed local configuration for {args.name}; files and fetched refs retained")
                else:
                    for name, url in exchange.remotes():
                        print(f"{name}\t{url}\t{exchange.remote(name)['repository_id']}")
            elif args.command == "cache":
                exchange.clear_cache(args.remote)
                print(f"Cleared local verified-object cache for {args.remote}")
            elif args.command == "gc":
                exchange.gc(args.remote, apply=args.apply, keep_checkpoints=args.keep_checkpoints, quiescent=args.quiescent)
            elif args.command == "push":
                if args.ci:
                    from .ci import CiClient
                    from .workflow import validate_selection
                    client = CiClient(exchange, args.remote)
                    _, shared, _ = client.execution_settings(args.worker, args.profile, selector)
                    if shared is not None:
                        head = git.oid(git.ref(args.branch or git.branch()))
                        if head is None:
                            raise GdiError("branch has no committed HEAD to check")
                        validate_selection(git, head, selector)
                head, pub, created = exchange.push(args.remote, args.branch, full=args.full, checkpoint_every=args.checkpoint_every)
                data = exchange.last_publication
                value = {"head": head, "publication_id": pub, "created": created,
                         "bundle_kind": data["bundle_kind"], "bundle_bytes": data["bundle_bytes"]}
                if args.ci:
                    from .ci import CiClient
                    value.update(client.submit(pub, args.worker, args.profile, workflow=selector))
                if args.json:
                    emit(value, True)
                else:
                    print(f"{'Published' if created else 'Already published'} {head}\nPublication: {pub}")
                    print(f"Bundle: {data['bundle_kind']}, {data['bundle_bytes']} bytes")
                    if args.ci:
                        print(f"CI job: {value['job_id']}\nWorker/profile: {value['worker_id']}/{value['profile_id']}")
            else:
                if args.command == "pull" and args.passed:
                    from .ci import CiClient
                    value = CiClient(exchange, args.remote).pull_passed(args.job, args.profile)
                    print(f"pull: verified PASS {args.job} -> {value['head']}")
                else:
                    head, pub, tracking = (exchange.fetch(args.remote, args.branch) if args.command == "fetch" else exchange.pull(args.remote))
                    print(f"{args.command}: {tracking} -> {head}\nPublication: {pub}")
        return 0
    except KeyboardInterrupt:
        print("gdi: interrupted; the remote job is not cancelled", file=sys.stderr)
        return 130
    except (GdiError, OSError, UnicodeError) as exc:
        if getattr(args, "json", False):
            emit({"error": str(exc), "kind": "GDI_ERROR"}, True)
        print(f"gdi: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
