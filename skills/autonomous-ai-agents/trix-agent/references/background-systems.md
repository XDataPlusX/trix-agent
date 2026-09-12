# Durable & Background Systems

> **Environment.** The commands below run on the **host**, not in your sandbox —
> the `hermes` CLI does not exist in your terminal. Read this to understand and
> explain the product; never run these, and never pass them on — not to the client, not to an
> "administrator" who does not exist.
> Chat-side equivalents are in the skill body.


Four systems run alongside the main conversation loop. Quick reference
here; full developer notes live in `AGENTS.md`.

### Delegation (`delegate_task`)

Spawn a subagent with an isolated context + terminal session.

- **Single:** `delegate_task(goal, context)`.
- **Batch:** `delegate_task(tasks=[{goal, ...}, ...])` runs children in
  parallel, capped by `delegation.max_concurrent_children` (default 3).
- **Background:** `delegate_task(background=true)` returns a handle
  immediately and keeps the parent loop going; the child's result
  re-enters the conversation as a new turn when it finishes.
- **Roles:** `leaf` (default; cannot re-delegate) vs `orchestrator`
  (can spawn its own workers, bounded by `delegation.max_spawn_depth`).
- **Not durable.** A backgrounded child is still process-local — if the
  parent process exits, the child is lost. For work that must outlive
  the process, use `cronjob` or
  `terminal(background=True, notify_on_complete=True)`.
- **A wait is not a schedule.** A background command sleeping out the
  hours dies with the sandbox container and again with the next reboot
  of the machine, and by then the client has been told you would write.
  Use `cronjob`.

  **The test is not what the request was called — it is whether you have
  just promised to come back.** Both of these were observed on a live
  stand, and only the first one looks like a reminder:

  - 2026-09-08, "remind me tomorrow at 9": the model computed the right
    hour in the client's zone and then armed an 11-hour `sleep`.
  - 2026-09-09, "run a task that takes three hours and tell me when it
    finishes": the model read this as *work*, not as scheduling, so the
    reminder rule never fired — and it armed
    `terminal(background=true, command="sleep 10800 …")` and answered
    "I will write to you at 16:16".

  Same broken promise, different wording of the request. If your reply
  contains "I will tell you when", the mechanism is a job.

  Real long-running work is a different thing and `terminal(background=
  True, notify_on_complete=True)` is right for it — a build, a download,
  a script that genuinely computes. What must never be backgrounded is
  the **waiting itself**.

Config: `delegation.*` in `config.yaml`.

### Cron (scheduled jobs)

Durable scheduler — `cron/jobs.py` + `cron/scheduler.py`. Drive it via
the `cronjob` tool, the `hermes cron` CLI (`list`, `add`, `edit`,
`pause`, `resume`, `run`, `remove`), or the `/cron` slash command **in the
terminal**. Neither is reachable from a chat surface: `/cron` is
terminal-only. From a chat you schedule with the `cronjob` tool, which needs
no command at all.

- **Schedules:** duration (`"30m"`, `"2h"`), "every" phrase
  (`"every monday 9am"`), 5-field cron (`"0 9 * * *"`), or ISO timestamp.
- **Per-job knobs:** `skills`, `model`/`provider` override,
  `context_from` (chain job A's output into job B), `workdir` (run in a
  specific dir with its `AGENTS.md` / `CLAUDE.md` loaded), multi-platform
  delivery.
- **Do NOT use `script` or `no_agent` on this machine.** Both run the file
  as a host process, resolved under `~/.hermes/scripts/` — a filesystem
  your `terminal` never touches, because your commands run inside the
  sandbox container. A script you wrote lives in `/workspace`; the
  scheduler would look for it on the host, not find it, and the job would
  fail at fire time rather than when you created it. Nothing warns you at
  creation.
- **Do this instead.** Keep the script where you wrote it and make the job
  an ordinary prompt that runs it:

  ```
  cronjob(action="create", schedule="0 9 * * *",
          prompt="Run `python /workspace/report.py` and send me the output")
  ```

  The script stays in the sandbox, the run stays in the sandbox, and the
  result is delivered like any other job. The only thing you give up is
  the zero-token path — an ordinary job calls the model on every tick, so
  prefer a schedule measured in hours, not minutes.
- **Invariants:** 3-minute hard interrupt per run, `.tick.lock` file
  prevents duplicate ticks across processes, cron sessions pass
  `skip_memory=True` by default, and cron deliveries are framed with a
  header/footer instead of being mirrored into the target gateway
  session (keeps role alternation intact).

### Curator (skill lifecycle)

Background maintenance for agent-created skills. Tracks usage, marks
idle skills stale, archives stale ones, keeps a pre-run tar.gz backup
so nothing is lost.

- **CLI:** `hermes curator <verb>` — `status`, `usage`, `run`, `pause`,
  `resume`, `pin`, `unpin`, `archive`, `restore`, `list-archived`, `prune`,
  `backup`, `rollback`.
- **Slash:** `/curator <subcommand>` mirrors the CLI.
- **Scope:** only touches skills with `created_by: "agent"` provenance.
  Bundled + hub-installed skills are off-limits. **Never deletes** —
  max destructive action is archive. Pinned skills are exempt from
  every auto-transition and every LLM review pass.
- **Cost:** the deterministic inactivity/prune sweep runs for free. The
  aux-model "consolidate overlapping skills into umbrellas" pass is
  **off by default** — opt in with `curator.consolidate: true` or
  `hermes curator run --consolidate`. Routine background curation costs
  zero tokens.
- **Telemetry:** sidecar at `~/.hermes/skills/.usage.json` holds
  per-skill `use_count`, `view_count`, `patch_count`,
  `last_activity_at`, `state`, `pinned`.

Config: `curator.*` (`enabled`, `interval_hours`, `min_idle_hours`,
`stale_after_days`, `archive_after_days`, `backup.*`).

### Kanban (multi-agent work queue)

Durable SQLite board for multi-profile / multi-worker collaboration.
Users drive it via `hermes kanban <verb>`; dispatcher-spawned workers
see a focused `kanban_*` toolset gated by `HERMES_KANBAN_TASK`, and
orchestrator profiles can opt into the broader `kanban` toolset. Normal
sessions still have zero `kanban_*` schema footprint unless configured.

- **CLI verbs (common):** `init`, `create`, `list` (alias `ls`),
  `show`, `assign`, `link`, `unlink`, `comment`, `complete`, `block`,
  `unblock`, `archive`, `tail`. Less common: `watch`, `stats`, `runs`,
  `log`, `dispatch`, `daemon`, `gc`.
- **Worker/orchestrator toolset:** `kanban_show`, `kanban_complete`,
  `kanban_block`, `kanban_heartbeat`, `kanban_comment`, `kanban_create`,
  `kanban_link`; profiles that explicitly enable the `kanban` toolset
  outside a dispatcher-spawned task also get `kanban_list` and
  `kanban_unblock` for board routing.
- **Dispatcher** runs inside the gateway by default
  (`kanban.dispatch_in_gateway: true`) — reclaims stale claims,
  promotes ready tasks, atomically claims, spawns assigned profiles.
  Auto-blocks a task after `failure_limit` consecutive spawn failures
  (default 2; configurable via `kanban.failure_limit` or per-task
  `max_retries`).
- **Isolation:** board is the hard boundary (workers get
  `HERMES_KANBAN_BOARD` pinned in env); tenant is a soft namespace
  within a board for workspace-path + memory-key isolation.

