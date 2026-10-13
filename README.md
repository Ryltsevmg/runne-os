# Runne OS

**Runne OS** — собственная сборка Linux: цельное рабочее окружение уровня Omarchy на стеке
**Arch Linux + Niri + Dank Material Shell**, с воспроизводимой установкой с live-образа и
агентами ИИ в системе по умолчанию.

*Runne OS is a coherent Arch Linux + Niri + DankMaterialShell desktop build with a
reproducible installer and AI agents on board. Project language: Russian.*

## Состав (публичная часть)

| Каталог | Что там |
|---|---|
| `packages/` | исходники пакетов `runneos-*`: метапакет рабочего стола, системные настройки (`/etc/skel`), темы, центр приложений, AI-центр, локальный пульт агентов |
| `iso/` | профиль archiso: live-образ с выбранным стеком и локальным репозиторием `[runneos]` |
| `installer/` | установщик на базе archinstall: обёртка `runneos-install` + постустановка `runneos-post-install` |
| `scripts/` | сборка пакетов и ISO, публикация репозитория |
| `tests/` | приёмочные сценарии и QEMU-стенд сквозной установки |

Внутренние протоколы проверок и слой знаний ИИ-агентов не публикуются.

## Статус

Рабочий стек проверен на двух машинах; ISO собирается, установка проходит end-to-end в QEMU
(2026-10-13), `runneos-agents` входит в образ. Открыто: реальное железо с LUKS, подпись
пакетов, интерактивное меню установщика.

## Сборка

```bash
./scripts/build-repo.sh          # собрать пакеты в out/repo (пользователь)
sudo ./scripts/build.sh          # собрать ISO в out/ (нужен archiso и root для mkarchiso)
tests/qemu/run-full-test.sh      # сквозная установка в QEMU (пользователь, KVM)
```

CI: GitHub Actions собирает пакеты на каждый push в `packages/` и публикует их как rolling
release `repo-current` — подключение:

```ini
[runneos]
SigLevel = Optional TrustAll
Server = https://github.com/Ryltsevmg/runne-os/releases/download/repo-current
```

Сборка ISO запускается вручную (`workflow_dispatch`) или по тегу `iso-*`; части образа
и sha256 прикладываются к релизу.

## Лицензия

[MIT](LICENSE). Сторонние компоненты сохраняют свои лицензии; источники тем WhiteSur
и отдельные вендоренные артефакты имеют собственные указания в своих каталогах.
