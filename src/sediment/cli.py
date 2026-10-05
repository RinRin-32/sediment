"""`sediment` command line. Thin: parse, build clients, call, print honestly.

Exit codes: 0 success, 1 the server or model refused/failed, 2 local problem
(config, bad arguments, validation), 130 interrupted. Server explanations are
printed verbatim via PebbleError.explain(); nothing is swallowed.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from sediment import __version__
from sediment.agent import Agent
from sediment.approval import Approver
from sediment.config import Config, ConfigError, load_config
from sediment.doctor import render_doctor
from sediment.edge import EdgeClient
from sediment.envelope import PebbleError, TransportError
from sediment.findings import (
    Transcript,
    default_title,
    load_transcript,
    save_transcript,
    summarize,
)
from sediment.gitinfo import head_commit, infer_repo, list_tree, toplevel
from sediment.jsonish import JSON, JSONObject, get_int, get_obj, get_str
from sediment.model import ChatModel, ModelError
from sediment.reporting import SkillReporter
from sediment.skills import (
    bundle_warnings,
    load_bundle,
    match_skills,
    render_skill_prompt,
    save_bundle,
)
from sediment.tools import Workspace
from sediment.ui import Console
from sediment.workstreams import WorkstreamClient

Handler = Callable[[argparse.Namespace, Config, Console], int]


def _ask(text: str) -> str:
    """Prompts go to stderr so stdout stays clean for model output."""
    sys.stderr.write(text)
    sys.stderr.flush()
    return input()


def _approver(config: Config, console: Console) -> Approver:
    return Approver(_ask, sys.stdin.isatty, console.say, config.tools.allow_always)


def _edge(config: Config) -> EdgeClient:
    pebble = config.require_pebble()
    return EdgeClient(pebble.url, pebble.token)


def _root(cwd: Path) -> Path:
    return toplevel(cwd) or cwd


# -- doctor -----------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, config: Config, console: Console) -> int:
    client = _edge(config)
    try:
        caps = client.capabilities(ws_id=args.ws_id)
    finally:
        client.close()
    console.result(render_doctor(config, caps))
    return 0


# -- skills -----------------------------------------------------------------


def cmd_skills_pull(args: argparse.Namespace, config: Config, console: Console) -> int:
    cwd = Path.cwd()
    repo = infer_repo(cwd, config.repo)
    max_tokens = args.max_tokens or config.max_skill_tokens
    console.say(f"pulling skills for repo {repo or '(none: pebble default)'} (max_tokens={max_tokens})")
    client = _edge(config)
    try:
        bundle = client.skills_pull(repo=repo, names=args.name or [], max_tokens=max_tokens)
    finally:
        client.close()
    path = save_bundle(config.cache_dir, repo, bundle)
    matches = match_skills(bundle.skills, list_tree(_root(cwd)))
    console.result(
        f"{len(bundle.skills)} skill(s), ~{bundle.token_estimate}/{bundle.token_budget} tokens; "
        f"cached at {path}"
    )
    for match in matches:
        state = "ACTIVE  " if match.active else "inactive"
        console.result(f"  {state} {match.skill.name}: {match.reason}")
    for warning in bundle_warnings(bundle, max_tokens):
        console.warn(warning)
    return 0


def cmd_skills_list(args: argparse.Namespace, config: Config, console: Console) -> int:
    cwd = Path.cwd()
    repo = infer_repo(cwd, config.repo)
    cached = load_bundle(config.cache_dir, repo)
    if cached is None:
        console.say(f"no skills cached for repo {repo or '(none)'}; run `sediment skills pull`")
        return 1
    pulled = time.strftime("%Y-%m-%d %H:%M", time.localtime(cached.pulled_at))
    console.result(f"repo {repo or '(none)'}: pulled {pulled} from pebble ({cached.path})")
    for match in match_skills(cached.bundle.skills, list_tree(_root(cwd))):
        state = "ACTIVE  " if match.active else "inactive"
        console.result(
            f"  {state} {match.skill.name} (~{match.skill.token_estimate} tokens): {match.reason}"
        )
    for warning in bundle_warnings(cached.bundle):
        console.warn(warning)
    return 0


def _settings_object(raw: JSON) -> JSONObject:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("skills/hook returned settings_json that is not a JSON object")
    return raw


def merge_hook_settings(existing: JSONObject, new: JSONObject) -> JSONObject:
    """Append new hooks per event, keep everything else the file already had."""
    merged = dict(existing)
    hooks = dict(get_obj(existing, "hooks") or {})
    for event, entries in (get_obj(new, "hooks") or {}).items():
        current = hooks.get(event)
        before = current if isinstance(current, list) else []
        added = entries if isinstance(entries, list) else [entries]
        hooks[event] = [*before, *added]
    merged["hooks"] = hooks
    for key, value in new.items():
        if key != "hooks":
            merged.setdefault(key, value)
    return merged


def cmd_skills_hook(args: argparse.Namespace, config: Config, console: Console) -> int:
    client = _edge(config)
    try:
        response = client.skills_hook(report_url=args.report_url)
    finally:
        client.close()
    settings = _settings_object(response.get("settings_json"))
    hours = get_int(response, "expires_hours")
    if args.out == "-":
        console.warn("printing hook config to stdout; it embeds a skills.report token.")
        console.result(json.dumps(settings, indent=2))
    else:
        out = Path(args.out)
        existing: JSONObject = {}
        if out.exists():
            loaded = json.loads(out.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{out} exists but is not a JSON object; not touching it")
            existing = loaded
        out.parent.mkdir(parents=True, exist_ok=True)
        # Restrict before the token is written, not after.
        out.touch(mode=0o600, exist_ok=True)
        out.chmod(0o600)
        out.write_text(json.dumps(merge_hook_settings(existing, settings), indent=2) + "\n")
        console.say(
            f"wrote hooks.PostToolUse to {out} (mode 600). It embeds a skills.report token "
            f"valid for {hours or '?'} hours; keep the file private."
        )
    note = get_str(response, "note")
    if note:
        console.say(f"pebble says: {note}")
    return 0


def cmd_skills_publish(args: argparse.Namespace, config: Config, console: Console) -> int:
    body = Path(args.file).read_text(encoding="utf-8")
    repo = infer_repo(Path.cwd(), config.repo)
    if not repo:
        raise ValueError("cannot publish without a repo; set `repo` in config or SEDIMENT_REPO")
    client = _edge(config)
    try:
        result = client.skills_publish(
            name=args.name,
            body=body,
            repo=repo,
            description=args.description,
            tags=args.tag,
            paths=args.path,
        )
    finally:
        client.close()
    verb = "updated" if result.get("updated") is True else "published"
    console.result(f"{verb} skill {args.name!r} in repo {get_str(result, 'repo', repo)}")
    for key in ("verdict", "policy_reason", "note"):
        if result.get(key):
            console.result(f"  {key}: {result.get(key)}")
    return 0


# -- kb ---------------------------------------------------------------------


def cmd_kb_search(args: argparse.Namespace, config: Config, console: Console) -> int:
    client = _edge(config)
    try:
        hits = client.kb_search(args.query, limit=args.limit, repo=args.repo)
    finally:
        client.close()
    if not hits:
        console.result("no results")
    for hit in hits:
        tags = f" [{', '.join(hit.tags)}]" if hit.tags else ""
        repo = f" ({hit.repo})" if hit.repo else ""
        console.result(f"{hit.score:6.2f}  {hit.title}  <{hit.kind}>{repo}{tags}")
        if hit.summary:
            console.result(f"        {hit.summary}")
    return 0


def cmd_kb_read(args: argparse.Namespace, config: Config, console: Console) -> int:
    client = _edge(config)
    try:
        note = client.kb_read(args.title)
    except PebbleError as exc:
        if exc.not_found:
            console.say(f"no KB note titled {args.title!r} (pebble: {exc.error})")
            return 1
        raise
    finally:
        client.close()
    meta = {k: v for k, v in note.items() if k not in {"ok", "body"}}
    console.result(json.dumps(meta, indent=2, ensure_ascii=False))
    console.result("")
    console.result(get_str(note, "body"))
    return 0


def _read_body(args: argparse.Namespace) -> str | None:
    if args.file == "-":
        return sys.stdin.read()
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    return args.body


def cmd_kb_write(args: argparse.Namespace, config: Config, console: Console) -> int:
    client = _edge(config)
    try:
        result = client.kb_write(
            title=args.title,
            body=_read_body(args),
            kind=args.kind,
            summary=args.summary,
            tags=args.tag,
            repo=args.repo or infer_repo(Path.cwd(), config.repo) or None,
            append=args.append,
            color=args.color,
        )
    finally:
        client.close()
    verb = "appended to" if result.get("appended") is True else "wrote"
    console.result(f"{verb} {get_str(result, 'title', args.title)} -> {get_str(result, 'path')}")
    return 0


def run_and_capture(argv: list[str], cwd: Path, echo: Callable[[str], None]) -> tuple[int, str, float]:
    """Run without a shell, echo output live, return (exit code, output, seconds)."""
    started = time.monotonic()
    with subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    ) as proc:
        assert proc.stdout is not None
        chunks = []
        for line in proc.stdout:
            echo(line.rstrip("\n"))
            chunks.append(line)
        code = proc.wait()
    return code, "".join(chunks), time.monotonic() - started


def cmd_kb_experiment(args: argparse.Namespace, config: Config, console: Console) -> int:
    argv = list(args.command or [])
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise ValueError("give the command to run after `--`, e.g. kb experiment -t T -- pytest -q")
    config.require_pebble()  # fail before running anything if we could not record it
    cwd = Path.cwd()
    command = shlex.join(argv)
    console.say(f"running: {command}")
    try:
        exit_code, output, seconds = run_and_capture(argv, cwd, console.say)
    except OSError as exc:
        console.say(f"could not start {argv[0]!r}: {exc}. No exit code was measured; nothing recorded.")
        return 2
    console.say(f"exit code {exit_code} after {seconds:.1f}s; recording to pebble")
    client = _edge(config)
    try:
        result = client.kb_experiment(
            title=args.title,
            command=command,
            exit_code=exit_code,
            hypothesis=args.hypothesis,
            output=output,
            duration_seconds=round(seconds, 3),
            repo=infer_repo(cwd, config.repo) or None,
            commit=head_commit(cwd),
        )
    finally:
        client.close()
    console.result(
        f"recorded {get_str(result, 'title', args.title)} -> {get_str(result, 'path')} "
        f"(verdict: {get_str(result, 'verdict', 'none given')})"
    )
    if len(output) > 4000:
        console.say("note: pebble keeps at most 4000 characters of the output.")
    return 0


def save_findings(
    config: Config, console: Console, transcript: Transcript, title: str | None, assume_yes: bool
) -> int:
    model = ChatModel(config.require_model(), notice=console.warn)
    try:
        note = summarize(model, transcript)
    finally:
        model.close()
    if not note:
        console.warn("the model produced an empty summary; nothing written")
        return 1
    request: JSONObject = {
        "title": title or default_title(transcript),
        "kind": "note",
        "repo": transcript.repo,
        "body": note,
    }
    if not assume_yes:
        decision = _approver(config, console).review("kb_write", request)
        if not decision.approved:
            console.say("not written to the KB.")
            return 1
        request = decision.arguments
    client = _edge(config)
    try:
        result = client.kb_write(
            title=get_str(request, "title"),
            body=get_str(request, "body"),
            kind=get_str(request, "kind", "note"),
            repo=get_str(request, "repo") or None,
        )
    finally:
        client.close()
    console.result(f"saved findings -> {get_str(result, 'path')}")
    return 0


def cmd_kb_save(args: argparse.Namespace, config: Config, console: Console) -> int:
    transcript = load_transcript(config.cache_dir, args.session)
    console.say(f"summarizing session {transcript.session_id} ({len(transcript.messages)} messages)")
    return save_findings(config, console, transcript, args.title, args.yes)


# -- chat -------------------------------------------------------------------


class ChatSession:
    """Everything a chat needs, built once, closed once."""

    def __init__(self, config: Config, console: Console, max_turns: int | None, use_skills: bool):
        cwd = Path.cwd()
        self.config = config
        self.console = console
        self.root = _root(cwd)
        self.repo = infer_repo(cwd, config.repo)
        self.session_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.model = ChatModel(config.require_model(), notice=console.warn)
        self._closers: list[Callable[[], None]] = [self.model.close]

        prompt, names = "", frozenset[str]()
        if use_skills:
            prompt, names = self._skills()
        reporter = self._reporter() if names else None
        self.agent = Agent(
            model=self.model,
            workspace=Workspace(self.root, config.tools),
            approver=_approver(config, console),
            console=console,
            max_turns=max_turns or config.tools.max_turns,
            skill_prompt=prompt,
            skill_names=names,
            reporter=reporter,
        )
        console.say(
            f"session {self.session_id[:8]} in {self.root} (repo {self.repo or '(none)'}); "
            f"mutating tools need your approval"
        )

    def _skills(self) -> tuple[str, frozenset[str]]:
        cached = load_bundle(self.config.cache_dir, self.repo)
        if cached is None:
            self.console.say("no skills cached for this repo (run `sediment skills pull`)")
            return "", frozenset()
        for warning in bundle_warnings(cached.bundle):
            self.console.warn(warning)
        matches = match_skills(cached.bundle.skills, list_tree(self.root))
        active = [m.skill for m in matches if m.active]
        skipped = [m.skill.name for m in matches if not m.active]
        self.console.say(
            f"skills active: {', '.join(s.name for s in active) or '(none)'}"
            + (f"; inactive (no path match): {', '.join(skipped)}" if skipped else "")
        )
        return render_skill_prompt(active), frozenset(s.name for s in active)

    def _reporter(self) -> SkillReporter | None:
        if not self.config.report_skill_use:
            return None
        try:
            pebble = self.config.require_pebble()
        except ConfigError:
            self.console.say("skill use will not be reported: pebble is not configured")
            return None
        edge = EdgeClient(pebble.url, pebble.token)
        reporter = SkillReporter(pebble.url, edge.skills_hook, self.session_id, self.repo)
        self._closers += [edge.close, reporter.close]
        return reporter

    def ask(self, text: str) -> None:
        result = self.agent.ask(text)
        path = save_transcript(
            self.config.cache_dir,
            Transcript(self.session_id, self.repo, self.started_at, self.agent.messages),
        )
        if not result.completed:
            self.console.say(f"(transcript saved to {path})")

    def transcript(self) -> Transcript:
        return Transcript(self.session_id, self.repo, self.started_at, self.agent.messages)

    def close(self) -> None:
        for close in self._closers:
            close()


def cmd_chat(args: argparse.Namespace, config: Config, console: Console) -> int:
    session = ChatSession(config, console, args.max_turns, not args.no_skills)
    try:
        if args.prompt:
            session.ask(" ".join(args.prompt))
        else:
            _repl(session)
        if args.note_findings:
            return save_findings(config, console, session.transcript(), None, assume_yes=False)
    finally:
        session.close()
    return 0


def _repl(session: ChatSession) -> None:
    session.console.say("type a request; /save writes findings to the KB; /exit or Ctrl-D quits")
    while True:
        try:
            line = _ask("sediment> ").strip()
        except EOFError:
            session.console.say("")
            return
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return
        if line == "/save":
            try:
                save_findings(session.config, session.console, session.transcript(), None, False)
            except PebbleError as exc:
                session.console.say(exc.explain())
            except (TransportError, ModelError, ConfigError) as exc:
                session.console.warn(f"findings not saved: {exc}")
            continue
        try:
            session.ask(line)
        except ModelError as exc:
            session.console.warn(f"model call failed: {exc}")
        except KeyboardInterrupt:
            session.console.say("(interrupted; the request was abandoned)")


# -- research (non-edge) ----------------------------------------------------

_STATUS_MEANING = {
    "queue_full": "pebble's queue for this workstream is full; the question was NOT accepted",
    "attachments_busy": "the workstream is busy with attachments; the question was NOT accepted",
}


def cmd_research(args: argparse.Namespace, config: Config, console: Console) -> int:
    pebble = config.require_pebble()
    question = " ".join(args.question)
    name = args.name or "research: " + (question[:60] + ("..." if len(question) > 60 else ""))
    client = WorkstreamClient(pebble.url, pebble.token)
    try:
        try:
            dispatch = client.dispatch(name, question)
        except PebbleError as exc:
            console.say(exc.explain())
            console.say(
                "  (research uses pebble's workstream API, which is not an edge route and "
                "needs the 'write' scope.)"
            )
            return 1
        console.result(f"ws_id: {dispatch.ws_id}")
        console.say(f"workstream {dispatch.name!r} on {pebble.url}; send status: {dispatch.status}")
        if not dispatch.accepted:
            console.warn(_STATUS_MEANING.get(dispatch.status, f"unexpected status {dispatch.status!r}"))
            return 1
        if args.follow:
            console.say("following events (Ctrl-C stops watching; the task keeps running)")
            outcome = client.follow(dispatch.ws_id, console.stream, timeout=args.timeout)
            console.end_stream()
            if not outcome.ended:
                console.warn(
                    f"live event stream unavailable: {outcome.problem}. The task is dispatched "
                    f"regardless; open {dispatch.ws_id} in the pebble console for results."
                )
    finally:
        client.close()
    return 0


# -- full access ------------------------------------------------------------


def cmd_full_access(args: argparse.Namespace, config: Config, console: Console) -> int:
    if args.action == "arm" and not args.yes:
        decision = _approver(config, console).review("arm-full-access", {"ws_id": args.ws_id})
        if not decision.approved:
            console.say("not armed.")
            return 1
    client = _edge(config)
    try:
        result = client.full_access(args.action, args.ws_id)
    finally:
        client.close()
    console.result(json.dumps({k: v for k, v in result.items() if k != "ok"}, indent=2))
    return 0


# -- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sediment", description="Edge client for pebble.")
    parser.add_argument("--version", action="version", version=f"sediment {__version__}")
    parser.add_argument("--config", type=Path, help="path to a config.toml")
    sub = parser.add_subparsers(dest="command")

    def add(
        name: str,
        handler: Handler,
        help_text: str,
        into: argparse._SubParsersAction[argparse.ArgumentParser] | None = None,
    ) -> argparse.ArgumentParser:
        p = (into or sub).add_parser(name, help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        return p

    chat = add("chat", cmd_chat, "run the local coding agent (REPL when no prompt is given)")
    chat.add_argument("prompt", nargs="*")
    chat.add_argument("--note-findings", action="store_true", help="summarize into the KB at the end")
    chat.add_argument("--no-skills", action="store_true", help="do not inject cached skills")
    chat.add_argument("--max-turns", type=int)

    doctor = add("doctor", cmd_doctor, "show who you are and what this token can do")
    doctor.add_argument("--ws-id")

    skills = sub.add_parser("skills", help="pull, list, publish skills; install the report hook")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    pull = add("pull", cmd_skills_pull, "fetch this repo's skill bundle and cache it", skills_sub)
    pull.add_argument("--name", action="append", help="only these skills (repeatable)")
    pull.add_argument("--max-tokens", type=int, help="lower the token budget")
    add("list", cmd_skills_list, "show cached skills and why each is active or not", skills_sub)
    hook = add("hook", cmd_skills_hook, "mint a Claude Code skill-report hook", skills_sub)
    hook.add_argument("--out", required=True, help="settings file to merge into, or - for stdout")
    hook.add_argument("--report-url")
    publish = add("publish", cmd_skills_publish, "publish a skill for this repo", skills_sub)
    publish.add_argument("name")
    publish.add_argument("--file", required=True, help="markdown body of the skill")
    publish.add_argument("--description")
    publish.add_argument("--tag", action="append")
    publish.add_argument("--path", action="append", help="glob this skill applies to (repeatable)")

    kb = sub.add_parser("kb", help="search, read and write pebble's knowledge base")
    kb_sub = kb.add_subparsers(dest="kb_command", required=True)
    search = add("search", cmd_kb_search, "search notes", kb_sub)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--repo")
    read = add("read", cmd_kb_read, "read one note by title", kb_sub)
    read.add_argument("title")
    write = add("write", cmd_kb_write, "write or append to a note", kb_sub)
    write.add_argument("title")
    write.add_argument("--body")
    write.add_argument("--file", help="read the body from a file, or - for stdin")
    write.add_argument("--append", action="store_true")
    write.add_argument("--kind")
    write.add_argument("--summary")
    write.add_argument("--tag", action="append")
    write.add_argument("--repo")
    write.add_argument("--color")
    experiment = add(
        "experiment", cmd_kb_experiment, "run a command and record its real exit code", kb_sub
    )
    experiment.add_argument("-t", "--title", required=True)
    experiment.add_argument("--hypothesis")
    experiment.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARGS...]")
    save = add("save", cmd_kb_save, "summarize a chat session into a KB note", kb_sub)
    save.add_argument("--session", help="session id (default: most recent)")
    save.add_argument("--title")
    save.add_argument("--yes", action="store_true", help="skip the review prompt")

    research = add("research", cmd_research, "dispatch a research question to pebble")
    research.add_argument("question", nargs="+")
    research.add_argument("--name")
    research.add_argument("--follow", action="store_true", help="stream events (SSE)")
    research.add_argument("--timeout", type=float, default=600.0)

    full = add("full-access", cmd_full_access, "arm, disarm or inspect full access on a session")
    full.add_argument("action", choices=["status", "arm", "disarm"])
    full.add_argument("ws_id")
    full.add_argument("--yes", action="store_true", help="arm without the confirmation prompt")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    if args.command is None:  # bare `sediment` is the REPL
        args = parser.parse_args([*(sys.argv[1:] if argv is None else argv), "chat"])
    try:
        config = load_config(explicit=args.config, env=os.environ)
        handler: Handler = args.handler
        return handler(args, config, console)
    except ConfigError as exc:
        console.say(f"config: {exc}")
        return 2
    except PebbleError as exc:
        console.say(exc.explain())
        return 1
    except (TransportError, ModelError) as exc:
        console.say(str(exc))
        return 1
    except (ValueError, OSError) as exc:
        console.say(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        console.say("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
