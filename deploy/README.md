# Production deployment — back.synora-ai.uz

Live at **https://back.synora-ai.uz** on `169.58.183.151` (Ubuntu 24.04), behind
nginx, run by systemd. This directory is the source of truth for the release
path; the files are installed to the paths below by `bootstrap.sh`.

| File | Server path |
| --- | --- |
| `deploy.sh` | `/usr/local/sbin/synora-api-deploy` |
| `synora-api.service` | `/etc/systemd/system/synora-api.service` |

nginx (`/etc/nginx/sites-available/back.synora-ai.uz.conf`) and the Let's Encrypt
certificate are set up once and are not touched by a deploy.

## How a deploy works

Pushing to `main` triggers [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml):
it runs `pytest`, and only if the suite is green SSHes in as `deploy` and runs
`/usr/local/sbin/synora-api-deploy` for that commit. The same script is what you
run by hand, so CI and manual deploys cannot drift.

`/opt/synora-backend` *is* the git checkout systemd runs — there is no build
output to stage, so a release is `git reset --hard <sha>`, `pip install -r
requirements.txt`, restart, then wait for `127.0.0.1:8010/health`. If the health
check fails, the script checks the previous commit back out, reinstalls, restarts
and prints the last 40 journal lines before failing the run.

Two things a deploy never touches, because both are gitignored and `git clean`
is run without `-x`:

- **`.env`** — the production `JWT_SECRET`. Overwriting it would sign every user
  out at once.
- **`data/synora.db`** — every account, until the database moves to Postgres;
  see [Moving the database to Postgres](#moving-the-database-to-postgres).
  `data/recordings/` outlives that move — it is the audio of every kept
  synthesis.

## Layout on the server

```
/opt/synora-backend/          git checkout, owned by `deploy`
├── .env                      synora:synora 0600 — never in git
├── .venv/                    the interpreter systemd runs
├── app/                      the code
├── data/synora.db            synora:synora 0750 — until the Postgres move
└── data/recordings/          synora:synora — audio of every kept synthesis
```

The service runs as `synora` with `ProtectSystem=strict`, so the process cannot
write to its own code; `ReadWritePaths=/opt/synora-backend/data` is the single
exception. It listens on `127.0.0.1:8010` only — 8000 belongs to the
online-talim container — and ufw allows just SSH, 80 and 443.

The `deploy` user owns the code and the venv, so a release needs no root. Its
only privileged rights are four `synora-api` commands, via
`/etc/sudoers.d/deploy-synora-api`.

## First-time setup

Once per server, as root:

```bash
scp -r deploy root@169.58.183.151:/tmp/synora-deploy
ssh root@169.58.183.151 bash /tmp/synora-deploy/bootstrap.sh
```

`bootstrap.sh` backs up `.env` and `data/` to `/root/synora-backend-backup-<utc>`,
turns `/opt/synora-backend` into a checkout of this repo, fixes ownership,
installs the release script and the sudoers rule, and syncs the unit file. It is
idempotent — re-run it after changing `deploy.sh` or `synora-api.service`.

## CI secret

The workflow needs one repository secret, `DEPLOY_SSH_KEY`: the private half of
the `ed25519` keypair at `/home/deploy/.ssh/github-actions`, whose public half is
already in `/home/deploy/.ssh/authorized_keys`. It is the same key the frontend
repo uses, so it can be copied from there, or read from the server:

```bash
ssh root@169.58.183.151 cat /home/deploy/.ssh/github-actions
```

The server address and its host key are pinned in the workflow itself.

## Common tasks

```bash
ssh deploy@169.58.183.151 /usr/local/sbin/synora-api-deploy   # deploy main by hand
ssh root@169.58.183.151 journalctl -u synora-api -f           # tail app logs
ssh root@169.58.183.151 systemctl status synora-api
```

Roll back to an earlier commit — the script health-checks it like any other
release, so a bad rollback target rolls itself forward again:

```bash
ssh deploy@169.58.183.151 'DEPLOY_REF=<sha> /usr/local/sbin/synora-api-deploy'
```

## Moving the database to Postgres

The production `.env` still points `DATABASE_URL` at SQLite, and
`assert_production_ready()` refuses to start on it — so every release since
`63e1c13` has failed its health check and rolled back. The refusal is correct:
SQLite serialises writers and silently ignores `FOR UPDATE`, so the row locking
the wallet depends on is a no-op there, and "twenty callers race for eight
credits" resolves by luck. It is fine for development and it is not a money
database.

Run with the API stopped. `data/synora.db` is not touched by any of this; keep
it until a day's charges have been read back from the new database.

```bash
# --- 1. a database and a role, as postgres -------------------------------
sudo -u postgres createuser --pwprompt synora
sudo -u postgres createdb --owner synora synora

# --- 2. stop the API. Nothing may write to either side during the copy ----
sudo systemctl stop synora-api

# --- 3. point .env at Postgres, and fix the other refusal while you are in it
sudo -u synora vim /opt/synora-backend/.env
#   DATABASE_URL=postgresql+asyncpg://synora:<password>@localhost/synora
#   INTERNAL_KEY_SECRET=<openssl rand -hex 32>   # must differ from JWT_SECRET
#   METRICS_TOKEN=<openssl rand -hex 32>         # optional; /metrics is 404 without it

# --- 4. build the schema on the target, at the same revision --------------
cd /opt/synora-backend
sudo -u synora .venv/bin/alembic upgrade head

# --- 5. copy, and prove it ------------------------------------------------
sudo -u synora .venv/bin/python devtools/migrate_to_postgres.py \
    --source sqlite+aiosqlite:///data/synora.db \
    --target "postgresql+asyncpg://synora:<password>@localhost/synora"

# --- 6. start, and check it is the migrated data ---------------------------
sudo systemctl start synora-api
curl -s 127.0.0.1:8010/health
```

Step 5 prints a row count per table from both sides and then recomputes every
wallet from its own ledger entries — the same `verify_all` reconciliation the
admin route runs. A mismatch in either exits non-zero and says so. **Do not
start the API on a target that failed the audit**; the recovery is to drop the
database and start again from step 1, which costs nothing while nothing has
been served from it.

It refuses a target that already has rows, and that refusal is load-bearing:
the natural response to a copy that died halfway is to run it again, and
`ledger_entries` carries an append-only trigger, so the duplicates could not be
deleted afterwards. Drop and recreate instead.

**`alembic_version` is not copied.** The target's revision comes from step 4,
which is also what makes the one-time `alembic stamp` below unnecessary on the
new database: it is built by migrations rather than inherited from whatever
state the old one was in.

**The pre-existing SQLite database may still need stamping first**, because
step 4 runs against Postgres but a later `alembic upgrade head` on a rollback
would run against whatever `.env` names. Check `alembic current` before
assuming — see the note under Known gaps.

Afterwards, `pg_dump` replaces the file copy that `bootstrap.sh` did, and
`data/` stops being the thing worth backing up — it holds recordings now
(`RECORDINGS_DIR`), which are worth keeping but are not money.

## Known gaps

- **The database has no backup.** `bootstrap.sh` takes one copy at setup time;
  nothing copies `data/synora.db` anywhere after that. On Postgres this becomes
  a `pg_dump` on a timer, and it is the same gap until somebody writes it.
- **A pre-existing database has to be stamped once, by hand.** The release now
  runs `alembic upgrade head` before the restart, so schema changes ship with
  the code that needs them. That only works on a database Alembic knows the
  state of. One that predates Alembic — built by `init_db()`'s `create_all` —
  has no `alembic_version` row, so the first migration tries to create tables
  that already exist and the release aborts. Fix it once, before the first
  deploy that carries a migration:

  ```bash
  ssh root@169.58.183.151
  cd /opt/synora-backend
  sudo -u synora .venv/bin/alembic current      # empty means never stamped
  sudo -u synora .venv/bin/alembic stamp 0001   # the pre-Alembic auth schema
  sudo -u synora .venv/bin/alembic upgrade head
  ```

  Note what `stamp 0001` asserts: that the database already contains exactly
  what revision `0001` creates. It is only true of a database that was serving
  the auth-only code. Check `alembic current` first rather than assuming.

- **Aborting mid-release leaves the schema forward.** The rollback puts the
  previous commit back but does not migrate down, on purpose: every revision
  here is additive, so older code ignores the new tables and runs correctly.
  A revision that dropped or rewrote a column would break that, and does not
  belong in an unattended release.

- **The database has no backup, and now it has a schema worth losing.** The
  point below has not changed, but the stakes have: `data/` now holds wallets
  and an append-only ledger, so losing it loses money that is owed rather than
  a table that can be rebuilt.
