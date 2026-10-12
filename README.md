# wtree

Create Git worktrees with their own URLs and databases. Docker services are shared.

## Disclaimer

Default detection and `dev` follow NestJS backend and Next.js frontend conventions, in a monorepo or standalone repo. Database migrations and seeds run your configured scripts and can use any ORM.

- Uses pnpm and requires Git, Python, Node.js, mise, Docker Compose, and Portless.
- Detects the default folder layout described below. Set `mapping` in the project config for custom paths.
- `dev` uses backend `start:dev` scripts, the shared backend's `build` script when present, and the Next.js CLI. Migration/seed scripts run at the repo root and handle their own build prerequisites.
- Runs apps on the host with shared PostgreSQL, Redis, LocalStack, and Mailpit services from `compose.yaml`.
- Copies env files and overrides managed connection values. App settings such as `EMAIL_PROVIDER` are preserved; your apps must support the generated env keys.

## Getting Started

Requires Git, mise, pnpm, Docker Compose, and Portless. Start Docker first.

1. From this folder, install the runtimes and copy the project config:

   ```sh
   mise install
   cp projects/template.json projects/my-repo.json
   ```

   Match the filename to your project's `origin` repository name, without `.git`. Edit the config for your project. Local configs are ignored by Git.

2. Add these aliases to `~/.zshrc`, adjust the path if needed, and open a new shell:

   ```sh
   alias wtree="mise exec python@3.12 node@22.21.0 -- python $HOME/Documents/code/dev-infra/wtree.py"
   alias base-local-infra="wtree infra"
   ```

3. From your project checkout, install dependencies and start development:

   ```sh
   mise exec node@22.21.0 -- pnpm install --frozen-lockfile
   wtree setup main
   wtree dev
   ```

By default, wtree finds apps in this monorepo layout. Apps that are absent are skipped:

```text
apps/server        NestJS server
apps/worker        NestJS worker
apps/web-app       Next.js web app
packages/backend   Shared backend build/watch package
```

If these runtime apps are absent, wtree checks the root `package.json`: `@nestjs/core` selects a standalone server; `next` selects a standalone web app. A `pnpm-workspace.yaml` file alone does not identify a monorepo. Set `mapping` when detection is ambiguous or your folders differ.

Each runtime app needs a local env file or `.env.example`. Run tests and other project commands through the repo's pnpm scripts from the worktree folder.

## Commands

Run project commands inside the checkout. From elsewhere, use `wtree --repo /path/to/checkout <command>`.

| Command | Action |
| --- | --- |
| `wtree create fix-tax --base development` | Create and set up a sibling checkout. |
| `wtree setup main` | Provision an existing checkout. |
| `wtree dev` | Start the mapped apps. |
| `wtree status` | Show URLs, ports, and databases. |
| `wtree remove` | Delete the checkout after ID confirmation. Keep its branch and databases. |
| `wtree stripe` | Save the signing secret. Restart the API afterward. |

Shared services: `base-local-infra up`, `status`, or `down`. Stopping them keeps volumes.

## Options

### Worktree options

| Option | Effect |
| --- | --- |
| `create <name> [path] --branch <branch> --base <ref>` | Defaults: sibling path, branch `<name>`, base `HEAD`. |
| `create <name> --existing --branch <branch>` | Use an existing branch. |
| `create` or `setup` with `--redis-namespace` | Separate Redis keys, channels, and queues by checkout and stage. |
| `remove --force` | Discard uncommitted changes and untracked files. |
| `remove --destroy-databases` | Also delete the checkout's dev/test databases. |

Stop servers and tests before removal. Ignored files and backups are deleted too. The main checkout is protected.

`wtree stripe` requires a server. Repo scripts own test preparation and any test worker databases. Existing `_1` databases from earlier wtree versions remain untouched, including by `remove --destroy-databases`; clean them up manually when no longer needed.

### Project config

Use [projects/template.json](projects/template.json). Only the template is included in Git.

Set `project` for hostnames, `prefix` for database names, and `bucket` for LocalStack S3. `databases` maps database URL env keys to scope suffixes.

`migrate_server`, `migrate_test`, and `seed` contain root pnpm script names. Setup runs development migrations, test migrations, then development seeds. Each completed script is recorded so retries skip completed steps. Use `[]` for an unused phase.

Optional: `has_websocket` and `tenants` (tenant IDs to hostname segments, including `platform`). Keep credentials and `EMAIL_PROVIDER` in local env files. Both `MAILHOG_*` and `MAILPIT_*` SMTP connection keys point to the shared service. Existing configs with `email_provider` are accepted; the field is ignored.

Setup preserves test tenant origins and unrelated env keys. Existing values that earlier versions overwrote stay as they are; use `.worktree/backups/` to restore them manually if needed.

### App mapping

Optional `mapping` defines the complete layout. It replaces detection. Paths are relative to the Git root and must point to folders with `package.json` inside the repo. Include at least one of `server`, `worker`, or `web`. `backend` is the optional shared build/watch package; `test` is derived automatically.

Add this field to the project config for a standalone backend, including `nestjs-mikro-orm-starter-template`:

```json
"mapping": { "server": "." }
```

For a standalone Next.js repo:

```json
"mapping": { "web": "." }
```

For custom monorepo folders:

```json
"mapping": {
  "server": "services/api",
  "worker": "services/jobs",
  "web": "frontend",
  "backend": "shared/backend"
}
```

Keep only the apps that exist. App commands use their mapped folders; configured migration and seed scripts run at the repo root. Web-only setup skips app databases, storage, migrations, and seeds. External API, web, and WebSocket URLs stay in the env files when the corresponding app is absent. There are no command-line path overrides.
