# Фикстуры адаптеров

Реальные строки JSONL/NDJSON, снятые живыми прогонами CLI на этой машине 2026-10-12:
**codex-cli 0.162.1** и **opencode v2.0.26**.

Отредактированы только идентификаторы (`thread_id`, `sessionID`, `prt_`/`msg_`/`call_`/`fc_`)
и пути; у `opencode_tool.jsonl` дополнительно убран внутренний блок `state.metadata` провайдера.
Тексты ответов, коды возврата, числа токенов и структура строк — как в прогоне.

| Файл | Что снято | Команда |
|---|---|---|
| `codex_start.jsonl` | новый ход, ответ `PONG` | `codex exec --json --skip-git-repo-check -C <dir> -s read-only -- 'ответь ровно PONG'` |
| `codex_resume_command.jsonl` | продолжение сессии и выполнение команды | `codex exec resume <thread_id> --json --skip-git-repo-check -c sandbox_mode=workspace-write -- 'выполни ровно команду echo PONG-RESUME и ответь ровно PONG'` |
| `codex_failure.jsonl` | сбой: предупреждение уровня item + конверты `error` и `turn.failed` | `codex exec … -m no-such-model -- 'ответь ровно PONG'` (код 1) |
| `codex_transient_retry.jsonl` | обрыв потока и восстановление: три конверта `error` «Reconnecting… N/5», затем успешный ответ `42` | `codex exec resume <thread_id> --json --skip-git-repo-check -- 'назови число 42'` (код 0) |
| `opencode_start.jsonl` | новый ход, ответ `PONG` | `opencode run --standalone --format json -- 'ответь ровно PONG'` |
| `opencode_resume.jsonl` | продолжение сессии, ответ `PONG2` | `opencode run --standalone --format json --session <id> -- 'ответь ровно PONG2'` |
| `opencode_tool.jsonl` | шаги, вызов инструмента `read`, usage по шагам, ответ `PONG3` | `opencode run --standalone --format json -- 'прочитай файл probe.txt в текущем каталоге и ответь ровно его содержимое'` |
| `opencode_error.jsonl` | сбой провайдера, код возврата 1 | `opencode run --format json --model no/such-model -- 'PONG'` |

Секретов и домашних путей в файлах нет: генерация проверяла это отдельно. Строки — источник
истины для `tests/test_agents_adapters.py`; если CLI провайдера сменит формат, тесты это покажут.
