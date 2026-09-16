# Команды в чате — ровно те, что есть у клиента

> **Environment.** Всё перечисленное здесь работает у клиента в чате.
> Команд консоли в этом файле нет вовсе и быть не должно: консоли у
> клиента нет, а `hermes` недостижим из вашей песочницы.

> **Этот список — весь набор.** Всё остальное, что вы могли бы вспомнить
> про команды продукта, у клиента **заблокировано шлюзом**: набранная
> руками такая команда не исполнится, а получит отказ. Поэтому здесь
> перечислено только то, что действительно работает, — предлагать что-то
> сверх этого списка нельзя.
>
> Список собран из реестра команд за вычетом заблокированных; сторож
> `tests/skills/test_slash_reference_matches_the_client.py` не даёт ему
> разойтись с кодом.

### Configuration
```
/fast [normal|fast|status] [--global] Toggle faster responses at a higher cost (normal/fast)
/model [model] [--provider name] [--global|--session] [--refresh] Switch model (session-scoped; --global to persist)
/reasoning [level|show|hide|full|clamp] [--global] Manage response thinking depth (cost/accuracy trade-off) and display
/setup                     Reopen the setup wizard — the bot sends a fresh address and one-time password
/voice [on|off|tts|status] Toggle voice mode
```

### Info
```
/commands [page]           Browse all commands and skills (paginated)
/debug                     Collect a diagnostic report as a file (versions, settings and log tails)
/disk [clean]              Show disk space; `clean` clears service files
/help                      Show available commands
/update                    Update Trix Agent to the latest version
/usage                     Show token usage and rate limits
/version                   Show Trix Agent version
```

### Session
```
/agents                    Show active agents and running tasks
/approve [session|always]  Approve a pending dangerous command
/background <prompt>       Run a prompt in the background
/compress [here [N] | focus topic | --preview|--dry-run] Compress a long conversation to reduce cost and context usage
/deny [all] [reason]       Deny a pending dangerous command (optionally with a reason)
/goal [text | draft <text> | show | gate add <cmd> | pause | resume | clear | status | wait <pid> | unwait] Set a standing goal the agent works on across turns until achieved
/heartbeat [every <interval> <prompt> | status | pause | resume | clear] Set a recurring prompt that re-enters this session when idle
/new [name]                Start a new conversation with a clean history
/queue <prompt>            Queue a prompt for the next turn (doesn't interrupt)
/restart                   Gracefully restart the agent after draining active runs
/resume [name]             Resume one of your previous sessions
/retry                     Retry the last message (resend to agent)
/sessions                  Browse and resume previous sessions
/sethome                   Set this chat as the home channel for scheduled tasks and cross-platform messages
/start                     Acknowledge platform start pings without a reply
/status                    Show session, model, token, and context info
/steer <prompt>            Inject a message after the next tool call without interrupting
/stop                      Interrupt the agent's current response and any background processes it started
/subgoal [text | remove N | clear] Add or manage extra criteria on the active goal
/title [name]              Set a title for the current session
/undo [N]                  Back up N user turns and re-prompt (default 1)
```

### Tools & Skills
```
/blueprint [name] [slot=value ...] Set up an automation from a blueprint template
/memory [pending|approve|reject|approval] [id|on|off] Review pending memory writes / toggle the approval gate
```

## Чего в этом списке нет

Трёх команд нет в меню Telegram, но набранные руками они работают:
`/start` (служебный пинг платформы), `/debug` (диагностический отчёт
файлом — просить его стоит только по просьбе поддержки) и `/sethome`.

Всё остальное — настройки, которые меняются в мастере `/setup`, либо
вопрос в поддержку XDataPlus. Команды консоли клиенту не называются
никогда: у него нет консоли.
