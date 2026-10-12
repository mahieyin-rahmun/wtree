#!/usr/bin/env python3
"""Shared Docker services and independent local Git worktrees. Python 3.12, standard library only."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

INFRA_ROOT = Path(__file__).resolve().parent
COMPOSE = ["docker", "compose", "-p", "shared-dev", "-f", str(INFRA_ROOT / "compose.yaml")]
PROXY_PORT = 1355
DEV_DATABASE_PORT, TEST_DATABASE_PORT = 5442, 5443
DATABASE_USER = DATABASE_PASSWORD = "postgres"
STORAGE_ORIGIN = "http://localhost:4566"
PORTLESS = shutil.which("portless") or next(
    (str(path) for path in (Path.home() / ".local/share/mise/installs/node").glob("*/bin/portless")), None)
APP_FOLDERS = {"server": "apps/server", "worker": "apps/worker",
               "web": "apps/web-app", "backend": "packages/backend"}


def resolve_config(repo, config):
    mapping = config.get("mapping")
    if "mapping" not in config:
        mapping = {role: folder for role, folder in APP_FOLDERS.items()
                   if (repo / folder / "package.json").is_file()}
        if not any(role in mapping for role in ("server", "worker", "web")):
            package = load_json(repo / "package.json", {})
            dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
            roles = [role for role, dependency in (("server", "@nestjs/core"), ("web", "next"))
                     if dependency in dependencies]
            if len(roles) != 1:
                raise RuntimeError(f"Cannot detect apps in {repo}. Set mapping in the project config.")
            mapping = {roles[0]: "."}
    if not isinstance(mapping, dict) or not mapping or any(role not in APP_FOLDERS for role in mapping):
        raise RuntimeError("mapping must map server, worker, web, or backend to repo-relative app paths.")
    if not any(role in mapping for role in ("server", "worker", "web")):
        raise RuntimeError("mapping needs at least one runtime app: server, worker, or web.")
    folders = {}
    for role, folder in mapping.items():
        if not isinstance(folder, str) or not folder or Path(folder).is_absolute():
            raise RuntimeError(f"mapping.{role} must be a repo-relative app path.")
        directory = (repo / folder).resolve()
        if not directory.is_relative_to(repo.resolve()) or not (directory / "package.json").is_file():
            raise RuntimeError(f"mapping.{role} must point to a package.json inside {repo}: {folder}")
        folders[role] = str(directory.relative_to(repo.resolve()))
    primary = next((role for role in ("server", "worker") if role in folders), None)
    if primary:
        folders["test"] = folders[primary]
    return {**config, "mapping": folders}


def backend_role(config):
    return next((role for role in ("server", "worker") if role in config["mapping"]), None)


def app_command(repo, config, role, *arguments):
    return ["pnpm", "--dir", str(repo / config["mapping"][role]), *arguments]


def run(arguments, *, cwd=None, env=None, input_text=None, has_capture=False, can_fail=False):
    return subprocess.run(arguments, cwd=cwd, env=env, input=input_text, text=True,
                          capture_output=has_capture, check=not can_fail)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def register(repo, config, name=None, has_redis_namespace=None):
    directory = INFRA_ROOT / ".state"
    directory.mkdir(exist_ok=True)
    with (directory / "registry.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "registry.json"
        state = load_json(path, {"worktrees": {}})
        entries = state["worktrees"]
        if str(repo) not in entries:
            used_slots = {entry["slot"] for entry in entries.values()}
            slot = 0
            while slot in used_slots or not can_use_ports(12000 + 100 * slot):
                slot += 1
                if slot > 350:
                    raise RuntimeError("No available worktree port blocks")
            branch = run(["git", "branch", "--show-current"], cwd=repo, has_capture=True).stdout.strip()
            label = re.sub(r"[^a-z0-9-]+", "-", (name or branch).lower()).strip("-")[:40].rstrip("-") or "worktree"
            worktree_id = hashlib.sha256(str(repo).encode()).hexdigest()[:8]
            if any(entry["project"] == config["project"] and entry["label"] == label for entry in entries.values()):
                label += "-" + worktree_id
            base = 12000 + 100 * slot
            entries[str(repo)] = {"repo": str(repo), "project": config["project"], "prefix": config["prefix"],
                                  "id": worktree_id, "label": label, "slot": slot, "is_ready": False,
                                  "ports": {"web": base, "api": base + 1, "ws": base + 2,
                                            "test_api": base + 9, "test_ws": base + 10}}
        entry = entries[str(repo)]
        if name:
            label = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40].rstrip("-") or "worktree"
            if any(other["project"] == config["project"] and other["label"] == label
                   and other["repo"] != str(repo) for other in entries.values()):
                label += "-" + entry["id"]
            entry["label"] = label
        if has_redis_namespace is not None:
            entry["has_redis_namespace"] = has_redis_namespace
        save_json(path, state)
        return entries[str(repo)]


def can_use_ports(base):
    try:
        for port in range(base, base + 100):
            with socket.socket() as connection:
                connection.bind(("127.0.0.1", port))
        return True
    except PermissionError:
        raise RuntimeError("Local socket permission is needed to choose worktree ports") from None
    except OSError:
        return False


def public_origin(hostname: str) -> str:
    return f"http://{hostname}:{PROXY_PORT}"


def env_file(repo, config, role):
    stage = "test" if role == "test" else "development"
    return repo / config["mapping"][role] / f".env.{stage}.local"


def discover_env_file(repo, config, role):
    stage = "test" if role == "test" else "development"
    names = [f".env.{stage}.local", f".env.{stage}"]
    if role != "test":
        names.append(".env.local")
    names.extend((".env", ".env.example"))
    directory = repo / config["mapping"][role]
    for name in names:
        path = directory / name
        if path.is_file():
            return path
    raise RuntimeError(f"No env file found in {directory}. Add .env.example or a stage env file.")


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    pattern = re.compile(r'''^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*(?:'((?:\\'|[^'])*)'|"((?:\\"|[^"])*)"|`([^`]*)`|([^\r\n#]*))''', re.MULTILINE)
    for match in pattern.finditer(path.read_text()):
        key, single_quoted, double_quoted, backtick_quoted, unquoted = match.groups()
        if single_quoted is not None:
            value = single_quoted
        elif double_quoted is not None:
            value = double_quoted.replace("\\n", "\n").replace("\\r", "\r")
        elif backtick_quoted is not None:
            value = backtick_quoted
        else:
            value = unquoted.strip()
        values[key] = value
    return values


def update_env(path, values, removed=()):
    lines = path.read_text().splitlines() if path.exists() else []
    remaining = dict(values)
    result = []
    for line in lines:
        key = line.removeprefix("export ").split("=", 1)[0].strip()
        if key in removed:
            continue
        if key in values:
            if key in remaining:
                result.append(f"{key}='{remaining.pop(key)}'")
        else:
            result.append(line)
    result.extend(f"{key}='{value}'" for key, value in remaining.items())
    path.write_text("\n".join(result) + "\n")
    path.chmod(0o600)


def database_names(entry: dict, config: dict, is_test: bool) -> dict[str, str]:
    if not backend_role(config):
        return {}
    stage = "test" if is_test else "dev"
    return {key: f"{entry['prefix']}_{entry['id']}_{stage}_{scope}" for key, scope in config["databases"].items()}


def routes(entry: dict, config: dict) -> dict[str, int]:
    base = f"{entry['label']}.{entry['project']}.localhost"
    result = {f"api.{base}": entry["ports"]["api"]} if "server" in config["mapping"] else {}
    if "web" in config["mapping"]:
        if config.get("tenants"):
            result.update({f"{host}.{base}": entry["ports"]["web"] for host in config["tenants"].values()})
        else:
            result[base] = entry["ports"]["web"]
    if "server" in config["mapping"] and config.get("has_websocket"):
        result[f"ws.{base}"] = entry["ports"]["ws"]
    return result


def build_environment(entry: dict, config: dict, role: str) -> dict[str, str]:
    is_test = role == "test"
    base = f"{entry['label']}.{entry['project']}.localhost"
    api = public_origin(f"api.{base}")
    web = public_origin(f"{'platform.' if config.get('tenants') else ''}{base}")
    stage = "test" if is_test else "local"
    namespace = f"{entry['project']}:{entry['id']}:{'test' if is_test else 'dev'}"
    origins = {tenant: public_origin(f"{host}.{base}") for tenant, host in config.get("tenants", {}).items()
               if "web" in config["mapping"]}
    if is_test:
        origins = {}
    values = {
        "NODE_ENV": "test" if is_test else "development", "STAGE_ENV": stage,
        "BE_PORT": str(entry["ports"].get("test_api", entry["ports"]["api"] + 8) if is_test else entry["ports"]["api"]), "API_BASE_URL": api,
        "API_HEALTH_URL": f"{api}/api/v1/health", "WEB_CLIENT_BASE_URL": web,
        "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "6379", "REDIS_PASSWORD": "password",
        "MAILHOG_HOST": "127.0.0.1", "MAILHOG_PORT": "1025",
        "MAILPIT_HOST": "127.0.0.1", "MAILPIT_PORT": "1025",
        "BLOB_STORAGE_S3_REGION": "us-east-1",
        "BLOB_STORAGE_S3_ENDPOINT": STORAGE_ORIGIN,
        "BLOB_STORAGE_S3_ACCESS_KEY": "test", "BLOB_STORAGE_S3_SECRET_KEY": "test",
        "STRIPE_WEBHOOK_FORWARD_URL": f"http://127.0.0.1:{entry['ports']['api']}/api/v1/payments/webhook",
    }
    if "server" not in config["mapping"]:
        for key in ("API_BASE_URL", "API_HEALTH_URL", "STRIPE_WEBHOOK_FORWARD_URL"):
            values.pop(key)
    if "web" not in config["mapping"]:
        values.pop("WEB_CLIENT_BASE_URL")
    if entry.get("has_redis_namespace"):
        values.update(REDIS_NAMESPACE=namespace, REDIS_QUEUE_PREFIX=namespace)
    port = TEST_DATABASE_PORT if is_test else DEV_DATABASE_PORT
    for key, name in database_names(entry, config, is_test).items():
        values[key] = f"postgresql://{DATABASE_USER}:{DATABASE_PASSWORD}@127.0.0.1:{port}/{name}?schema=public"
    if "server" in config["mapping"] and config.get("has_websocket"):
        values["BE_WS_PORT"] = str(entry["ports"]["test_ws"] if is_test else entry["ports"]["ws"])
        values["E2E_WS_PORT_BASE"] = str(entry["ports"]["test_ws"])
    if origins:
        values["TENANT_WEB_ORIGINS"] = json.dumps(origins, separators=(",", ":"))
    if role == "web":
        values = {
            "NODE_ENV": "development", "PORT": str(entry["ports"]["web"]),
            "NEXT_PUBLIC_STAGE_ENV": "local",
        }
        if "server" in config["mapping"]:
            values.update(NEXT_PUBLIC_API_BASE_URL=f"{api}/api/v1", SWAGGER_URL=f"{api}/swagger-json")
        if origins:
            values["NEXT_PUBLIC_TENANT_WEB_ORIGINS"] = json.dumps(origins, separators=(",", ":"))
        if config.get("bucket"):
            values["NEXT_PUBLIC_BLOB_STORAGE_S3_BUCKET_URL"] = f"{STORAGE_ORIGIN}/{config['bucket']}"
        if "server" in config["mapping"] and config.get("has_websocket"):
            values["NEXT_PUBLIC_WS_BASE_URL"] = f"ws://ws.{base}:{PROXY_PORT}"
    return values


def managed_environment(entry, config, role):
    values = build_environment(entry, config, role)
    if role != "web":
        values.update(BLOB_STORAGE_S3_BUCKET_NAME=config["bucket"],
                      BLOB_STORAGE_S3_BUCKET_URL=f"{STORAGE_ORIGIN}/{config['bucket']}")
    return values


def child_environment(repo, entry, config, role):
    values = {**os.environ, **read_env(discover_env_file(repo, config, role)), **managed_environment(entry, config, role)}
    if not entry.get("has_redis_namespace"):
        values.pop("REDIS_NAMESPACE", None)
    return values


def query(is_test, sql, database="postgres", *, cwd=None):
    service = "test_postgres" if is_test else "dev_postgres"
    return run([*COMPOSE, "exec", "-T", service, "psql", "-U", "postgres", "-d", database,
                "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"], cwd=cwd, input_text=sql, has_capture=True).stdout.strip()


def ensure_database(name, is_test):
    if query(is_test, f"SELECT 1 FROM pg_database WHERE datname = '{name}';") != "1":
        query(is_test, f'CREATE DATABASE "{name}";')


def proxy(entry=None, config=None):
    if not PORTLESS:
        raise RuntimeError("Install Portless once: mise exec -- pnpm add -g portless")
    run([PORTLESS, "proxy", "start", "--no-tls", "--port", str(PROXY_PORT)])
    if entry:
        for host, port in routes(entry, config).items():
            run([PORTLESS, "alias", host.removesuffix(".localhost"), str(port)])


def storage(config):
    def aws(*arguments, can_fail=False):
        return run([*COMPOSE, "exec", "-T", "localstack", "awslocal", *arguments],
                   has_capture=True, can_fail=can_fail)

    bucket = config["bucket"]
    if aws("s3api", "head-bucket", "--bucket", bucket, can_fail=True).returncode:
        aws("s3api", "create-bucket", "--bucket", bucket)
    policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": "*",
              "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{bucket}/*"}]}
    cors = {"CORSRules": [{"AllowedOrigins": ["*"], "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "PUT", "POST", "DELETE", "HEAD"], "ExposeHeaders": ["ETag"]}]}
    aws("s3api", "put-bucket-policy", "--bucket", bucket, "--policy", json.dumps(policy))
    aws("s3api", "put-bucket-cors", "--bucket", bucket, "--cors-configuration", json.dumps(cors))


def setup(repo, entry, config, source=None, source_config=None):
    source_config = source_config or config
    roles = [role for role in ("server", "test", "worker", "web") if role in config["mapping"]]
    sources = {}
    for role in roles:
        if not env_file(repo, config, role).exists():
            if source and role in source_config["mapping"]:
                sources[role] = discover_env_file(source, source_config, role)
            else:
                sources[role] = discover_env_file(repo, config, role)
    proxy(entry, config)
    run([*COMPOSE, "up", "-d", "--wait"])
    for role in roles:
        path = env_file(repo, config, role)
        if not path.exists():
            shutil.copyfile(sources[role], path)
        backup = repo / ".worktree/backups" / path.relative_to(repo)
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, backup)
            backup.chmod(0o600)
        values = managed_environment(entry, config, role)
        if not entry.get("has_redis_namespace") and role != "web":
            original = read_env(backup).get("REDIS_QUEUE_PREFIX", read_env(path.parent / ".env.example").get("REDIS_QUEUE_PREFIX"))
            if original and re.fullmatch(r"[a-z-]+:[a-f0-9]+:(dev|test)", original):
                original = read_env(path.parent / ".env.example").get("REDIS_QUEUE_PREFIX")
            if original:
                values["REDIS_QUEUE_PREFIX"] = original
        removed = () if entry.get("has_redis_namespace") else ("REDIS_NAMESPACE",)
        update_env(path, values, removed)
    for is_test in (False, True):
        for name in database_names(entry, config, is_test).values():
            ensure_database(name, is_test)
    primary = backend_role(config)
    if primary:
        storage(config)
    path = repo / ".worktree/initialization.json"
    progress = load_json(path, {})
    commands = [(role, command, f"{stage}:{command}") for stage, role in (("server", primary), ("test", "test"))
                for command in config[f"migrate_{stage}"]] if primary else []
    if primary and not progress.get("is_imported"):
        commands += [(primary, command, f"seed:{command}") for command in config["seed"]]
    for role, command, key in commands:
        if not progress.get(key):
            run(["pnpm", "run", command], cwd=repo, env=child_environment(repo, entry, config, role))
            progress[key] = True
            save_json(path, progress)
    entry["is_ready"] = True
    registry_path = INFRA_ROOT / ".state/registry.json"
    with registry_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_json(registry_path, {})
        state["worktrees"][str(repo)] = entry
        save_json(registry_path, state)
    status(entry, config)


def status(entry, config):
    print(f"{entry['project']}/{entry['label']} ({entry['id']})")
    for host, port in routes(entry, config).items():
        print(f"  {public_origin(host)} -> localhost:{port}")
    for is_test in (False, True):
        names = database_names(entry, config, is_test)
        if names:
            print(f"  {'Test' if is_test else 'Dev'}: " + ", ".join(names.values()))


def remove(repo, entry, config, should_destroy, should_force=False):
    git_directory = Path(run(["git", "rev-parse", "--absolute-git-dir"],
                             cwd=repo, has_capture=True).stdout.strip()).resolve()
    common_directory = Path(run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=repo, has_capture=True).stdout.strip()).resolve()
    if git_directory == common_directory:
        raise RuntimeError("Cannot remove the main checkout. Run remove from a linked worktree.")
    names = []
    print(f"Worktree to delete: {repo}\nAll files in this checkout, including ignored files, will be deleted.")
    if should_force:
        print("Uncommitted changes and untracked files will also be deleted.")
    if should_destroy:
        names = [(name, is_test) for is_test in (False, True)
                 for name in database_names(entry, config, is_test).values()]
        print("Databases to delete:\n" + "\n".join(name for name, _ in names))
    if input(f"Type {entry['id']} to confirm removal: ") != entry["id"]:
        print("Removal cancelled.")
        return
    command = ["git", "worktree", "remove"]
    if should_force:
        command.append("--force")
    command.append(str(repo))
    run(command, cwd=common_directory)
    for host in routes(entry, config):
        run([PORTLESS, "alias", "--remove", host.removesuffix(".localhost")], cwd=INFRA_ROOT, can_fail=True)
    registry_path = INFRA_ROOT / ".state/registry.json"
    with registry_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_json(registry_path, {"worktrees": {}})
        state["worktrees"].pop(str(repo), None)
        save_json(registry_path, state)
    for name, is_test in names:
        query(is_test, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);', cwd=INFRA_ROOT)
    print("Worktree, files, routes, and registry entry removed. Databases "
          + ("deleted." if should_destroy else "preserved."))


def stripe(repo, entry, config):
    env = child_environment(repo, entry, config, "server")
    env["STRIPE_API_KEY"] = env["STRIPE_SECRET_KEY"]
    name = f"wtree-stripe-{entry['id']}"
    process = subprocess.Popen(["docker", "run", "--rm", "--name", name, "--env", "STRIPE_API_KEY",
                                "stripe/stripe-cli:v1.42.0", "listen", "--latest", "--forward-to",
                                f"http://host.docker.internal:{entry['ports']['api']}/api/v1/payments/webhook"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in process.stdout:
            secret = re.search(r"whsec_[A-Za-z0-9]+", line)
            if secret:
                update_env(env_file(repo, config, "server"), {"STRIPE_WEBHOOK_SECRET": secret.group()})
                print("Signing secret saved. Restart your API.", flush=True)
            else:
                print(line.rstrip(), flush=True)
        return process.wait()
    finally:
        if process.poll() is None:
            run(["docker", "stop", name], can_fail=True)


def dev(repo, entry, config):
    proxy(entry, config)
    status(entry, config)
    commands = [(role, app_command(repo, config, role, "run", "start:dev"))
                for role in ("server", "worker") if role in config["mapping"]]
    if "backend" in config["mapping"]:
        run(app_command(repo, config, "backend", "run", "build"), cwd=repo)
        commands.append((backend_role(config) or "web", app_command(repo, config, "backend", "run", "start:dev")))
    if "web" in config["mapping"]:
        commands.append(("web", app_command(repo, config, "web", "exec", "next", "dev", "-p", str(entry["ports"]["web"]))))
    processes = [subprocess.Popen(command, cwd=repo, env=child_environment(repo, entry, config, role),
                                  start_new_session=True) for role, command in commands]
    try:
        return processes[0].wait()
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    infra = commands.add_parser("infra")
    infra.add_argument("action", choices=("up", "down", "status"))
    create = commands.add_parser("create")
    create.add_argument("name", help="Worktree name and URL prefix")
    create.add_argument("destination", nargs="?", type=Path)
    create.add_argument("--branch", help="Defaults to the worktree name")
    create.add_argument("--base", default="HEAD")
    create.add_argument("--existing", action="store_true")
    create.add_argument("--redis-namespace", action="store_true")
    provision = commands.add_parser("setup")
    provision.add_argument("name", nargs="?")
    provision.add_argument("--redis-namespace", action="store_true")
    for name in ("status", "dev", "stripe"):
        commands.add_parser(name)
    cleanup = commands.add_parser("remove")
    cleanup.add_argument("--destroy-databases", action="store_true")
    cleanup.add_argument("--force", action="store_true", help="Discard uncommitted changes and untracked files")
    args = parser.parse_args()
    if args.command == "infra":
        if args.action == "up":
            proxy()
            run([*COMPOSE, "up", "-d", "--wait"])
        elif args.action == "down":
            run([*COMPOSE, "stop"])
            run([PORTLESS, "proxy", "stop", "--port", str(PROXY_PORT)])
        else:
            run([*COMPOSE, "ps"])
        return
    repo = Path(run(["git", "rev-parse", "--show-toplevel"], cwd=args.repo,
                    has_capture=True).stdout.strip()).resolve()
    remote = run(["git", "remote", "get-url", "origin"], cwd=repo, has_capture=True).stdout.strip()
    repository = remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    config_path = INFRA_ROOT / "projects" / f"{repository}.json"
    config = load_json(config_path, {})
    if not config:
        raise RuntimeError(f"Add this project's configuration at {config_path}")
    project_config = config
    config = resolve_config(repo, project_config)
    if args.command == "stripe" and "server" not in config["mapping"]:
        raise RuntimeError("stripe requires a mapped server app.")
    if args.command == "create":
        destination = args.destination.resolve() if args.destination else repo.parent / args.name
        branch = args.branch or args.name
        command = ["git", "worktree", "add"]
        command += [str(destination), branch] if args.existing else ["-b", branch, str(destination), args.base]
        run(command, cwd=repo)
        destination_config = resolve_config(destination, project_config)
        run(["pnpm", "install", "--frozen-lockfile"], cwd=destination)
        setup(destination, register(destination, destination_config, args.name, args.redis_namespace),
              destination_config, repo, config)
        print(f"\ncd {destination}\nRun the same Python alias with 'dev' to start it.")
        return
    if args.command == "setup":
        entry = register(repo, config, args.name, args.redis_namespace)
        setup(repo, entry, config)
        return
    if args.command == "remove":
        entry = load_json(INFRA_ROOT / ".state/registry.json", {"worktrees": {}})["worktrees"].get(str(repo))
        if not entry:
            raise RuntimeError(f"Checkout is not registered: {repo}")
        remove(repo, entry, config, args.destroy_databases, args.force)
        return
    entry = register(repo, config)
    if args.command == "status":
        status(entry, config)
    elif args.command == "dev":
        return dev(repo, entry, config)
    elif args.command == "stripe":
        return stripe(repo, entry, config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
