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
- **`data/synora.db`** — every account.

## Layout on the server

```
/opt/synora-backend/          git checkout, owned by `deploy`
├── .env                      synora:synora 0600 — never in git
├── .venv/                    the interpreter systemd runs
├── app/                      the code
└── data/synora.db            synora:synora 0750 — the only writable path
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

## Known gaps

- **The database has no backup.** `bootstrap.sh` takes one copy at setup time;
  nothing copies `data/synora.db` anywhere after that.
- **Schema changes are not migrated.** `init_db()` only creates missing tables,
  so a deploy that alters an existing model needs the `ALTER TABLE`s (or Alembic)
  applied by hand — the health check will pass while the queries fail.
