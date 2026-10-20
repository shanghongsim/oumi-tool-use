# Multi-turn tool-calling evaluation on Oumi

This repo runs the retail domain of [τ²-bench](https://github.com/sierra-research/tau2-bench) as a multi-turn tool-calling evaluation on the Oumi platform. It's the first step toward training a small, self-hosted tool-calling model. 

Oumi supplies the agent machinery: tool implementations, a simulated customer, and LLM judges. τ²-bench supplies a real store database, real tool code, a written policy, and tasks with known correct outcomes. No tool output is simulated: every tool call runs τ²'s retail code, so results can be checked exactly.

## How it works

```
Oumi evaluation
  ├─ model under test ── tool call ─────────────┐
  ├─ simulated customer (gpt-4.1)               │
  └─ judges (score the finished transcript)     │
                                                ▼
        endpoint tool implementations (one per tool)
        POST <args as JSON> + X-Oumi-Session-Id header
                                                │  public HTTPS
                                                ▼
        Cloudflare tunnel ──► tool_server.py on your machine
                                                │  one fresh store per run + session
                                                ▼
                         τ² retail tools + database (500 users, 1,000 orders)
```

- **Run and session isolation.** `run_evaluation.py` gives each run its own URL, `/runs/<run tag>/tools/<tool>`, by creating 16 implementations for it, and the platform sends a session ID with every call. The server keeps a separate copy of the store for each run and session, so a cancellation only shows up in that conversation, and two runs never share stores. [LOU-4417](https://linear.app/oumi/issue/LOU-4417) proposes a platform run ID that would remove the need for per-run implementations.
- **Refusals reach the model.** A 2xx response body is the tool's output. When the store refuses a call, for example cancelling a delivered order, the server answers `400 {"error": "..."}`. The model sees that as the tool's error and has to recover.

## Repo layout

| Path | What it is |
|---|---|
| `.env.example` | Environment variables to copy into `.env` |
| `requirements.txt` | Python packages needed on top of τ²-bench |
| `tau2_retail/convert_tasks.py` | Turns τ² retail tasks into Oumi rows: policy as the system prompt, a generic opening message, the 16 tool definitions, and metadata (`user_persona`, `expected_actions`, `required_statements`, `tau2_task_id`) |
| `tau2_retail/tool_server.py` | FastAPI server exposing each τ² retail tool at `POST /runs/<run tag>/tools/<name>`, one store per run and session |
| `tau2_retail/test_tool_server.py` | Local check: replays every task's correct actions through the server and compares the store with τ²'s; also checks auth, refusals and session isolation |
| `tau2_retail/setup_platform.py` | Creates the dataset, 16 tools, 16 endpoint implementations and 2 judges in your Oumi project; writes `platform_ids.json` |
| `tau2_retail/platform_ids.json` | IDs of what `setup_platform.py` created in your project; `run_evaluation.py` reads it. Git-ignored, since each person's project differs |
| `tau2_retail/run_evaluation.py` | Starts a multi-turn evaluation for one model, with its own run tag and 16 implementations; records the run in `runs.json` (git-ignored) |
| `tau2_retail/cleanup_run.py` | Lists runs, or deletes a finished run's implementations |
| `tau2_retail/export_results.py` | Downloads a finished evaluation's conversations |
| `tau2_retail/score_conversations.py` | τ²'s exact store check, run locally |
| `tau2_retail/summarize_results.py` | Writes `per_task.csv` and `exact_scores.txt` for a results folder and prints the headline numbers |
| `tau2_retail/call_stats.py` | Summarizes the tool server's call log |
| `tau2_retail/evaluators.md` | The two judges, described in plain words |
| `tau2_retail/data/` | Converted datasets: test (40 tasks), train (74), a 5-task trial set, and `tools.json` |
| `tau2_retail/results/` | Results per model |
| `tau2_retail/logs/` | Server log, tunnel log and the tool-call log (`calls.jsonl`). Git-ignored |
## Install

You need macOS or Linux, [uv](https://docs.astral.sh/uv/), Python 3.12 (uv can install it), `cloudflared` (`brew install cloudflared`), and an Oumi API key and project.

```bash
git clone <this repo> oumi-tool-use && cd oumi-tool-use

# τ²-bench, pinned to the commit these results used
git clone --depth 1 https://github.com/sierra-research/tau2-bench.git sources/tau2-bench
git -C sources/tau2-bench fetch --depth 1 origin 96a3ef92329baf908ffe9dfe188cea34e8701d2a
git -C sources/tau2-bench checkout 96a3ef92329baf908ffe9dfe188cea34e8701d2a

uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -e sources/tau2-bench -r requirements.txt

cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # use as TOOL_SERVER_API_KEY
# then fill in OUMI_API_KEY and OUMI_PROJECT_ID (and OUMI_API_URL if not production)
```

The original [τ-bench](https://github.com/sierra-research/tau-bench) (commit `59a200c6d575d595120f1cb70fea53cef0632f6b`) is only needed later, for its 500 extra retail training tasks.

## Run it

Run everything from `tau2_retail/` with the environment loaded:

```bash
cd tau2_retail
set -a; source ../.env; set +a
```

1. **Convert the tasks.** The converted files are already committed; re-run this only if you change the conversion.
   `../.venv/bin/python convert_tasks.py data`
2. **Test the server locally.** Expect `gold replays matching tau2: 114/114`.
   `../.venv/bin/python test_tool_server.py`
3. **Start the server and tunnel**, in two terminals with the environment loaded:
   ```bash
   mkdir -p logs
   TOOL_SERVER_CALL_LOG=logs/calls.jsonl ../.venv/bin/uvicorn tool_server:app --host 127.0.0.1 --port 8765 2>&1 | tee -a logs/server.log
   cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765 2>&1 | tee -a logs/tunnel.log
   ```
   Put the printed `https://….trycloudflare.com` URL into `TOOL_SERVER_URL` in `../.env`, reload the environment, and check it with `curl "$TOOL_SERVER_URL/healthz"`.
4. **Set up the platform.**
   `../.venv/bin/python setup_platform.py data/tau2_retail_test.jsonl`
   This creates the dataset, 16 tools, 16 endpoint implementations and 2 judges, sends two test calls, and writes `platform_ids.json`. It's safe to re-run, because it skips anything that already exists by name.
   The 16 implementations it creates point at the server's default run (`/tools/<tool>`) and are only used for those test calls and manual checks. Evaluations get their own (step 5). A quick tunnel gets a new URL every time it restarts and an implementation's URL can't be edited, so these setup implementations go stale after a tunnel restart; that doesn't affect evaluations.
5. **Run an evaluation.**
   ```bash
   ../.venv/bin/python run_evaluation.py ANTHROPIC_API claude-sonnet-5.5
   ../.venv/bin/python run_evaluation.py PRETRAINED google/gemma-4-E2B-it
   ```
   Each run creates its own implementations, checks them with one test call, starts the evaluation, and prints the run tag and operation ID. API models start immediately; self-hosted models wait for a GPU first. Add `--no-launch` to create and check a run's implementations without starting an evaluation.
6. **Watch it.**
   `python3 call_stats.py` (calls per run, then a summary), `python3 call_stats.py --run <run-tag>` (one run), `python3 call_stats.py --session <session-id-prefix>` (one conversation's calls), `tail -f logs/calls.jsonl` (live).
7. **Download the results and score them exactly.**
   ```bash
   ../.venv/bin/python export_results.py <operation_id> results/<model>
   ../.venv/bin/python summarize_results.py results/<model>
   ```
   `summarize_results.py` writes `per_task.csv` and `exact_scores.txt` and prints the headline numbers. For just the exact check on any conversations file, use `score_conversations.py <conversations.jsonl>`.
   Judge verdicts are in the Oumi web app and in each row's `metadata.judge`.
8. **Clean up the run's implementations** once its evaluation has finished, so they don't pile up in the Tools pages. The tools themselves stay.
   ```bash
   ../.venv/bin/python cleanup_run.py              # list runs
   ../.venv/bin/python cleanup_run.py <run-tag>    # delete that run's 16 implementations
   ```

## Deploy a shared Lambda endpoint

The laptop + Cloudflare tunnel flow above is the fastest way to experiment. For
a shared development environment, deploy the included AWS SAM application from
the repository root:

```bash
sam build --template-file tau2_retail/template.yaml
sam deploy --guided \
  --template-file tau2_retail/template.yaml \
  --parameter-overrides EnvironmentName=development EndpointToken='<generate-a-secret>'
```

This builds a Lambda container image with the τ² commit pinned by this repo and
creates a DynamoDB table with TTL cleanup. Configure `TOOL_SERVER_URL` with the
stack's `ToolFunctionUrl` output. The endpoint routes used by
`run_evaluation.py` work unchanged: `/runs/<run-tag>/tools/<tool-name>`.

Before deployment, validate the state algorithm without AWS credentials:
`../.venv/bin/python test_lambda_runtime.py` (from `tau2_retail/`).

The deployed service deliberately stores **successful mutating tool calls**, not
the complete τ² retail database. The retail database is approximately 2.8 MiB,
which exceeds DynamoDB's 400 KiB item limit. On every call, Lambda starts from
the bundled canonical database and replays the write log for that `(run, session)`.
This preserves τ²'s deterministic behavior, keeps evaluation runs isolated, and
uses `X-Oumi-Call-Id` to replay a completed mutating call rather than applying it
twice after a retry.

## Scoring

| Check | What it measures | Where |
|---|---|---|
| Correct changes (judge) | Whether the agent's successful change and transfer calls match `expected_actions` exactly | Oumi |
| Required statements (judge) | Whether the agent told the customer what τ² requires (its `nl_assertions`) | Oumi |
| Exact store match | Replays the agent's calls on a fresh store and compares with τ²'s correct final store: τ²'s database reward | `score_conversations.py` |

The judges and the exact check can disagree for three reasons:

- **Transfers.** `expected_actions` includes `transfer_to_human_agents` when τ² lists it, but the store check ignores transfers because they don't change the store.
- **ID typos.** An LLM judge can miss a one-digit difference in a 10-digit ID; the store check can't.
- **Refused calls.** The store check only looks at the final state, so refused calls never count. The judge is told to ignore them.

## Results so far

| Model | Exact store match | Correct changes | Required statements | Notes |
|---|---|---|---|---|
| claude-sonnet-5.5 | 39/40 (97.5%) | 37/40 | 40/40 | [summary](tau2_retail/results/claude-sonnet-5.5/summary.md) |
| google/gemma-4-E2B-it | 16/40 (40%) | 15/40 | 37/40 | [summary](tau2_retail/results/gemma-4-E2B-it/summary.md); mostly fails by transferring to a human instead of acting. An earlier run was invalid and is kept in `invalid-run-3597/` |

Settings: the simulated customer is gpt-4.1 at temperature 0 (τ²-bench's default), the model under test runs at temperature 0, conversations allow at most 20 customer turns, and the judges are claude-sonnet-5.5 at temperature 0. These are single runs, not directly comparable to the τ² leaderboard; see the summary.

## Platform limits worth knowing

- **Customer turns:** at most 20 per conversation, including the opening message.
- **Assistant steps:** at most 20 in a row, where each tool call or reply is a step. The count resets when the customer speaks.
- **One domain per evaluation:** the tools attached to an evaluation are given to every row.
- **Opening message only:** each row keeps only its first user message; the simulated customer writes the rest, guided by `user_persona`.
- **Endpoint requirements:** tool endpoints must be public HTTPS, redirects aren't followed, and responses must be JSON of at most 1 MiB.
- **Error handling:** a 4xx response (except 408 and 429) is shown to the model as the tool refusing the call. 5xx responses, 408, 429 and timeouts count as failures.

## Credits

τ-bench and τ²-bench are by Sierra Research and MIT-licensed. The files in `tau2_retail/data/` are derived from τ²-bench's retail domain (policy, tools and tasks).
