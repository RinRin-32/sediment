# sediment

An **edge client for [pebble](#architecture)**. pebble is the central server; sediment
is what runs on your laptop, next to the code. It is a small coding-agent CLI plus
thin commands for pebble's skills, knowledge base (KB) and research.

It is built the way pebble is meant to be used: calm, honest status, explicit
approvals, nothing hidden. sediment never fakes a result, never drops a warning
quietly, and never says something was run or tested when it wasn't.

## Architecture

```
 laptop (edge)                                   pebble (central)
 ┌───────────────────────────────┐   HTTPS      ┌──────────────────────────────┐
 │ sediment                      │  Bearer tok  │ /v1/api/edge/*               │
 │  agent loop ── model endpoint │ ───────────▶ │   capabilities, skills/*,    │
 │  local tools (read/write/run) │              │   kb/*, sessions/*           │
 │  approvals (human, local)     │              │ /v1/api/workstreams/* (*)    │
 │  skill path filtering         │              │ /v1/api/skills/report (*)    │
 │  skill cache, transcripts     │              │ orchestration, storage,      │
 └───────────────────────────────┘              │ policy, token budgets        │
                                                └──────────────────────────────┘
 (*) not edge routes; see "Known gaps"
```

**What lives where**

| Concern | Where | Why |
|---|---|---|
| Model calls, tool execution, approvals | edge | The code and the human are on the laptop. |
| Which skills a repo has; token budget; policy on publish | pebble | Shared across people and machines. |
| Whether a skill's `paths` globs match | edge | pebble explicitly does not see your working tree, so only the edge can answer. sediment checks each skill's `paths` with `fnmatch` against `git ls-files` (tracked + untracked, not ignored) and records why each skill is active or not. |
| KB notes and experiment records | pebble | Team memory. sediment writes; pebble stores and judges (`verdict`). |
| Research orchestration | pebble | sediment only dispatches and optionally watches. |

**Modules** (`src/sediment/`), each with a docstring that says why it exists:

- `edge.py` covers the documented `/v1/api/edge/*` routes and nothing else.
- `workstreams.py` holds the **non-edge** workstream routes used by `research`, kept on their own.
- `reporting.py` holds the **non-edge** skill-use report and pulls the token out of the hook command.
- `envelope.py` turns the `{"ok": ...}` envelope and HTTP errors into `PebbleError`, keeping the server's words verbatim.
- `config.py` loads the TOML config and applies env overrides. Tokens are only ever shown redacted.
- `agent.py`, `model.py`, `tools.py`, `approval.py` make up the local agent loop.
- `skills.py`, `gitinfo.py` handle path filtering, the cache and repo inference.
- `findings.py` saves session transcripts and writes the summary back to the KB.
- `doctor.py`, `cli.py`, `ui.py` are the user-facing commands.

## Quickstart

```sh
uv sync                       # Python >= 3.11; creates .venv with the `sediment` entrypoint
uv run sediment --help
```

### Config

sediment loads the first file it finds, in this order (files are not merged):

1. `--config PATH`
2. `$SEDIMENT_CONFIG` (also accepted under the spelling `$SEDIAMENT_CONFIG`)
3. `./sediment.toml`
4. `$XDG_CONFIG_HOME/sediment/config.toml` (default `~/.config/sediment/config.toml`)

```toml
repo = "my-repo"                  # optional; otherwise inferred from `git remote get-url origin`

[pebble]
url = "https://pebble.example.com"
token = "..."                     # never printed; logs show "abcd... (len 40)"

[model]                           # any OpenAI-compatible /chat/completions endpoint
base_url = "http://localhost:8000/v1"   # include the /v1 (or equivalent) prefix
api_key = ""                            # optional
name = "your-model"

[skills]
max_tokens = 30000                # may lower pebble's budget, never raise it
report = true                     # report skill use (see Known gaps)
# cache_dir = "~/.cache/sediment"

[tools]
max_turns = 20                    # model calls per request before sediment stops and says so
command_timeout = 120             # seconds; the whole process group is killed after this
max_output_chars = 20000          # tool output sent to the model is clipped, with a note saying so
allow_always = true               # set false to remove the "always" approval option
```

Environment overrides: `SEDIMENT_PEBBLE_URL`, `SEDIMENT_PEBBLE_TOKEN`,
`SEDIMENT_MODEL_BASE_URL`, `SEDIMENT_MODEL_API_KEY`, `SEDIMENT_MODEL`, `SEDIMENT_REPO`,
`SEDIMENT_MAX_SKILL_TOKENS`, `SEDIMENT_MAX_TURNS`, `SEDIMENT_CACHE_DIR`. If a setting is
missing, only the commands that need it fail, with a message that says where sediment looked
and what to set.

### doctor: what can this token do?

```sh
sediment doctor
```

This calls `GET /v1/api/edge/capabilities` and prints your `user_id`, scopes, capabilities and
every edge operation with `[yes]`/`[NO ]` and what is missing. It also lists the non-edge
routes sediment uses, because `capabilities` does not cover them.

### chat

```sh
sediment chat "find why test_parse is flaky"   # one request
sediment                                       # REPL; /save writes findings, /exit quits
sediment chat --note-findings "..."            # summarize into a KB note at the end
```

Model text goes to stdout. Everything sediment itself says (approvals, tool activity,
warnings) goes to stderr. Streaming is used when the endpoint supports it. If the endpoint
refuses streaming, sediment says so once and continues without it. Transcripts are saved to
`<cache_dir>/sessions/<id>.json`.

### skills

```sh
sediment skills pull                 # fetch this repo's bundle, filter by paths, cache it
sediment skills pull --name a --name b --max-tokens 10000
sediment skills list                 # what's active and why ("glob 'src/**/*.py' matched src/x.py")
sediment skills hook --out .claude/settings.json   # merge a PostToolUse report hook (file mode 600)
sediment skills publish my-skill --file SKILL.md --path 'src/**/*.py'
```

`truncated` and `not_found` from pebble are printed as `WARNING: TRUNCATED ...` /
`WARNING: NOT FOUND ...` on every pull, every `list`, and at the start of every chat.
A bundle that was quietly cut down to fit the budget is exactly the failure this avoids.

Active skills are put into the agent's system prompt. When the model applies one, it calls
`use_skill`, and sediment reports the use to pebble. If reporting fails, you see a warning
and the session carries on.

### kb

```sh
sediment kb search "flaky parser" --limit 5
sediment kb read "Parser notes"
sediment kb write "Parser notes" --append --kind note --tag parser --body "..."
sediment kb write "Long note" --file notes.md          # or --file - for stdin
sediment kb experiment -t "lockfile fixes flake" --hypothesis "..." -- pytest -q tests/test_parse.py
sediment kb save                                       # summarize the latest chat session
```

`kb experiment` **runs the command itself** (no shell). It shows the output as it runs and
records the real exit code, output, duration, repo and `HEAD` commit. If the command can't
be started, no exit code was measured and nothing is recorded. The client also refuses any
experiment whose `exit_code` is not a real integer; it never defaults one.

`kb save` / `--note-findings` asks the model to summarize the session, then shows you the
exact note through the normal approval prompt before writing it (kind `note`, attributed
to the repo). `--yes` skips the review.

### research

```sh
sediment research "why did p95 latency double last week?"
sediment research --follow "..."     # stream content frames over SSE until stream_end
```

This creates a workstream, sends the question, and prints `ws_id` so you can open it in the
pebble console. `queue_full` / `attachments_busy` mean the question was **not** accepted;
sediment says so and exits 1. If `--follow` loses the SSE stream, sediment says so and
exits 0: the task was dispatched either way. If pebble refuses, you see the scope your token
lacks.

### full access

```sh
sediment full-access status WS_ID
sediment full-access arm WS_ID       # asks for confirmation; --yes to skip
sediment full-access disarm WS_ID
```

These need the `write` scope and the `full_access` capability, and only work on your own
sessions (other sessions come back as 404).

## Approvals

- `read_file`, `list_dir` and `search` run without asking, but each call and a preview of
  its result is printed. All paths are confined to the workspace (the git toplevel, or the
  current directory).
- `write_file` and `run_command` **always** need an explicit yes. The prompt shows the tool
  name and every argument in full: the entire command text and the entire file content,
  never a summary.
- Answers: `y` approves. `N` (the default; just press Enter) denies. `e` edits the
  arguments; the edited call is shown again before you approve, and the model is told it
  was edited. `a` approves this tool for the rest of the session (turn it off with
  `tools.allow_always = false`).
- End of input, Ctrl-C at the prompt, or a non-interactive stdin all count as **deny**.
- A denial goes back to the model as the tool result: `DENIED: the human did not approve
  this ... It was NOT executed and nothing changed.` The model is never left thinking
  something ran.
- Each request is capped at `tools.max_turns` model calls. Hitting the cap is reported as
  an unfinished task, not a finished one.

Commands you type yourself (`kb experiment -- CMD`) are your own explicit action and aren't
prompted again. Arming full access and saving model-written findings both go through the
approval prompt.

## Known gaps in the edge API

These are worked around here, not hidden:

1. **No raw report token from `skills/hook`.** Reporting skill use
   (`POST /v1/api/skills/report`, body `{name, session_id, repo}`) needs a token with the
   `skills.report` scope, and read/write tokens get a 403. The only way to get one is
   `skills/hook`, which returns it embedded in a curl command inside `settings_json`. sediment
   extracts it with a tolerant parser: it accepts an object or a JSON string, and either quote
   style. If there is no literal token, or more than one, it fails with a clear error. The
   token is minted lazily on first `use_skill` and kept only in memory. A format change on
   pebble's side would break this, which is why it is a gap.
2. **No edge route to create a workstream or send a message.** `research` uses the
   non-edge workstream API (`/v1/api/workstreams/new`, `/{ws_id}/send`). That needs the
   `write` scope, is not covered by edge authorization, and does not show up in `doctor`'s
   capability table. The code lives in its own module (`workstreams.py`) so it can't be
   mistaken for an edge call.
3. **No way to read a workstream's transcript over the edge API.** `research` can't poll for
   results. The only live view is the SSE `events` stream (`--follow`). If that drops, the
   answer has to be read in the pebble console.
4. **No per-repo ACL.** Any token with read access can pull any repo's skills, and
   `repo` is just a string the client sends. sediment infers it honestly (config, then git
   origin name, then checkout directory name), but pebble does not and cannot check that you
   actually work on that repo.
5. **Two response contracts on one host.** The `{"ok": true, ...}` / `{"ok": false, "error": ...}`
   envelope is promised only for `/v1/api/edge/*`. The workstream API answers in an older shape
   with no `ok` field at all (e.g. `{"ws_id", "name", "message_count", "resumed"}` from
   `workstreams/new`, `{"status"}` from `/send`). `/v1/api/skills/report` has no documented
   response shape. Applying the edge rule everywhere once made `research` reject every
   successful dispatch. So each request in sediment now states its contract
   (`request_json(..., expect_envelope=...)`, with no default):
   - **Edge calls** still require a 2xx *and* `ok: true`. A 200 with `ok` false or missing is
     an error.
   - **Non-edge calls** succeed on any 2xx with a JSON-object or empty body. An explicit
     `"ok": false` is still treated as a failure, and a 2xx that isn't JSON (a proxy or login
     page) is refused.

   If you add a route, decide which surface it is on before you call it.

Smaller things worth knowing:

- `capabilities` describes edge routes only, so `doctor` cannot confirm ahead of time that
  `research` or skill reporting will be allowed.
- The repo naming convention pebble expects (`name` vs `org/name`) is not specified. sediment
  sends the bare repository name. Set `repo` in config if your pebble uses another form.
- pebble keeps at most 4000 characters of experiment output; sediment sends what it captured
  and tells you when it was longer.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
```

Tests use `httpx.MockTransport` for both pebble and the OpenAI-compatible model. Nothing
touches the network. CI runs on Python 3.11 and 3.13 (`.github/workflows/ci.yml`).
