import contextlib
import copy
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import wtree


class WtreeRemovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.registry = self.repo / "registry.json"
        self.branch = "fix/removal"
        self.key = wtree.entry_key(self.repo, self.branch)
        self.entry = {
            "repo": str(self.repo), "branch": self.branch,
            "project": "example", "prefix": "example", "id": "12345678",
            "label": "fix-removal", "slot": 0, "is_ready": False,
            "ports": {"api": 12001, "web": 12000},
        }
        self.config = {
            "mapping": {"server": ".", "test": ".", "web": "."},
            "databases": {"DATABASE_URL": "shared"},
            "migrate_server": ["migrate:dev"], "migrate_test": ["migrate:test"],
            "seed": ["seed:dev"],
        }
        self.setups = [(self.key, self.entry), ("seed:seed:dev", copy.deepcopy(self.entry))]
        self.addCleanup(patch.stopall)
        patch.object(wtree, "REGISTRY_PATH", self.registry).start()
        patch("sys.stdout", new=io.StringIO()).start()

    def save_setups(self):
        wtree.save_json(self.registry, {"worktrees": dict(self.setups)})

    def test_setup_saves_readiness_under_branch_key_after_migrations_and_seed(self):
        self.save_setups()
        with contextlib.ExitStack() as stack:
            for name in ("env_sources", "proxy", "run", "write_env_files", "ensure_database",
                         "storage", "child_environment", "status"):
                stack.enter_context(patch.object(wtree, name))
            wtree.setup(self.repo, self.key, self.entry, self.config)
        entries = wtree.load_json(self.registry, {})["worktrees"]
        self.assertTrue(entries[self.key]["is_ready"])
        self.assertNotIn("seed:seed:dev", {
            key for key, entry in entries.items() if entry["is_ready"]
        })
        progress = wtree.load_json(wtree.progress_path(self.repo, self.entry), {})
        self.assertEqual(progress, {
            "server:migrate:dev": True, "test:migrate:test": True, "seed:seed:dev": True,
        })

    def test_register_repairs_duplicate_and_keeps_readiness_and_other_branches(self):
        self.setups[1][1]["is_ready"] = True
        other = {**self.entry, "branch": "another", "id": "87654321"}
        self.setups.append((wtree.entry_key(self.repo, "another"), other))
        self.save_setups()
        with patch.object(wtree, "current_branch", return_value=self.branch):
            key, entry = wtree.register(self.repo, self.config)
        self.assertEqual(key, self.key)
        self.assertTrue(entry["is_ready"])
        entries = wtree.load_json(self.registry, {})["worktrees"]
        self.assertEqual(set(entries), {self.key, wtree.entry_key(self.repo, "another")})

    def test_duplicate_setups_remove_each_route_and_database_once(self):
        self.save_setups()
        with patch.object(wtree, "run") as run, patch.object(wtree, "query") as query:
            wtree.unregister(self.setups, self.config, True)
        self.assertEqual(run.call_count, len(wtree.routes(self.entry, self.config)))
        self.assertEqual(query.call_count, 2)
        self.assertEqual(wtree.load_json(self.registry, {}), {"worktrees": {}})

    def test_database_checks_preserve_both_stages_for_duplicate_names(self):
        with patch.object(wtree, "query") as query:
            wtree.check_databases(self.setups, self.config)
        self.assertEqual(query.call_args_list, [
            call(False, "SELECT 1;", cwd=wtree.INFRA_ROOT),
            call(True, "SELECT 1;", cwd=wtree.INFRA_ROOT),
        ])

    def test_query_reports_postgres_error(self):
        error = subprocess.CalledProcessError(1, ["docker"], stderr='service "dev_postgres" is not running')
        with patch.object(wtree, "run", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'dev_postgres.*is not running'):
                wtree.query(False, "SELECT 1;")

    def git_result(self, arguments, **kwargs):
        if "--absolute-git-dir" in arguments:
            return subprocess.CompletedProcess(arguments, 0, str(self.repo / ".git/worktrees/fix"))
        if "--git-common-dir" in arguments:
            return subprocess.CompletedProcess(arguments, 0, str(self.repo / ".git"))
        return subprocess.CompletedProcess(arguments, 0, self.branch)

    def test_remove_stops_before_git_removal_when_postgres_is_unavailable(self):
        with patch.object(wtree, "run", side_effect=self.git_result) as run, \
                patch.object(wtree, "query", side_effect=RuntimeError("Postgres stopped")), \
                patch.object(wtree, "unregister") as unregister, \
                patch("builtins.input", return_value=self.entry["id"]):
            with self.assertRaisesRegex(RuntimeError, "Postgres stopped"):
                wtree.remove(self.repo, self.setups, self.config, True)
        self.assertFalse(any(args[0][:3] == ["git", "worktree", "remove"] for args, _ in run.call_args_list))
        unregister.assert_not_called()

    def test_remove_without_database_destruction_works_when_postgres_is_stopped(self):
        with patch.object(wtree, "run", side_effect=self.git_result) as run, \
                patch.object(wtree, "query") as query, \
                patch.object(wtree, "unregister") as unregister, \
                patch("builtins.input", return_value=self.entry["id"]):
            wtree.remove(self.repo, self.setups, self.config, False)
        query.assert_not_called()
        unregister.assert_called_once_with(self.setups, self.config, False)
        self.assertEqual(run.call_args.args[0], ["git", "worktree", "remove", str(self.repo)])

    def test_cleanup_stops_before_unregister_when_postgres_is_unavailable(self):
        with patch.object(wtree, "query", side_effect=RuntimeError("Postgres stopped")), \
                patch.object(wtree, "unregister") as unregister, \
                patch("builtins.input", return_value=self.entry["id"]):
            with self.assertRaisesRegex(RuntimeError, "Postgres stopped"):
                wtree.cleanup(self.repo, self.key, self.entry, self.config, True)
        unregister.assert_not_called()

    def test_cancelled_removal_does_not_access_postgres(self):
        with patch.object(wtree, "run", side_effect=self.git_result), \
                patch.object(wtree, "query") as query, \
                patch.object(wtree, "unregister") as unregister, \
                patch("builtins.input", return_value="cancel"):
            wtree.remove(self.repo, self.setups, self.config, True)
        query.assert_not_called()
        unregister.assert_not_called()


if __name__ == "__main__":
    unittest.main()
