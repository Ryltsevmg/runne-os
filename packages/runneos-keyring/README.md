# runneos-keyring

Пакет с публичным ключом подписи репозитория `[runneos]`. Ставится в ISO и в
установленные системы; `post_install` выполняет `pacman-key --populate runneos`,
поэтому после установки пакета pacman проверяет подпись базы и пакетов
(`SigLevel = Required DatabaseOptional`).

## Ключ

- Отпечаток: `3174AC28EBBBD790813B4FBC00B5A884A68486E9`
- UID: `RunneOS packages <packages@runneos.local>`
- ed25519, только подпись, без срока, без пароля (нужен для ежедневной
  автоматической публикации таймером)
- Приватный ключ живёт **только на машине публикации** (minipc) в выделенном
  доме ключей `~/.local/state/runneos/pkg-signing/gnupg` (0700), не в личном
  `~/.gnupg`. Сертификат отзыва — рядом, `openpgp-revocs.d/`. Приватный ключ
  не коммитить, не копировать в ISO и не выводить в отчёты.

## Подпись при публикации

`scripts/publish-repo.sh` подписывает базу и пакеты, когда заданы:

```bash
RUNNEOS_GPG_KEY=3174AC28EBBBD790813B4FBC00B5A884A68486E9 \
RUNNEOS_GNUPGHOME=~/.local/state/runneos/pkg-signing/gnupg \
  ./scripts/publish-repo.sh
```

Ту же пару переменных получают юниты `runneos-publish-repo.{service,timer}`.

## Ротация ключа

1. Создать новый ключ в том же выделенном GNUPGHOME (рецепт в
   `references/repository.md`).
2. Заменить `files/runneos.gpg` и `files/runneos.asc`, обновить отпечаток в
   `PKGBUILD` (check), в тесте `tests/test_pkg_keyring.py`, в таймере и в этой
   справке; поднять `pkgrel`.
3. Опубликовать репозиторий с новым ключом; старый ключ отозвать сертификатом
   отзыва, выложив его в репозиторий/релиз.

## Клиенты

Машины с пакетом проверяют подпись автоматически. Для машины без пакета:
`sudo pacman-key --add <runneos.asc> && sudo pacman-key --lsign-key
3174AC28…86E9`, затем в `/etc/pacman.conf` для `[runneos]` поставить
`SigLevel = Required DatabaseOptional`. Публичный GitHub-канал (`repo-current`)
собирается в CI без приватного ключа и остаётся неподписанным — там сохраняется
`Optional TrustAll`.
