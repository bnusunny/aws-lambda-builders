import json
import os
import shutil
import tempfile
from unittest import TestCase

from aws_lambda_builders.workflows.nodejs_npm.lockfile_closure import production_closure


class TestProductionClosure(TestCase):
    """
    Feeds production_closure a synthetic workspace tree: two members with disjoint dependencies,
    a shared workspace package, and a version conflict (the shared package nests its own copy of
    a package one member also depends on).
    """

    def setUp(self):
        self.project_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.project_root, ignore_errors=True)

        self.lock = {
            "name": "mono",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "mono", "workspaces": ["functions/*", "packages/*"]},
                "functions/fn-a": {"dependencies": {"@mono/shared": "^1.0.0", "lodash": "^4.17.20"}},
                "functions/fn-b": {"dependencies": {"@mono/shared": "^1.0.0", "axios": "^1.6.0"}},
                "packages/shared": {"version": "1.0.0", "dependencies": {"lodash": "4.17.15"}},
                "node_modules/@mono/fn-a": {"link": True, "resolved": "functions/fn-a"},
                "node_modules/@mono/fn-b": {"link": True, "resolved": "functions/fn-b"},
                "node_modules/@mono/shared": {"link": True, "resolved": "packages/shared"},
                "node_modules/lodash": {"version": "4.17.20"},
                "node_modules/axios": {"version": "1.6.0", "dependencies": {"follow-redirects": "^1.15.0"}},
                "node_modules/follow-redirects": {"version": "1.15.6"},
                "packages/shared/node_modules/lodash": {"version": "4.17.15"},
                "node_modules/eslint": {"version": "9.0.0", "dev": True},
            },
        }
        for key in self.lock["packages"]:
            if key:
                os.makedirs(os.path.join(self.project_root, *key.split("/")), exist_ok=True)
        self.lockfile_path = self._write_lockfile(self.lock)

    def _write_lockfile(self, lock):
        path = os.path.join(self.project_root, "package-lock.json")
        with open(path, "w") as handle:
            json.dump(lock, handle)
        return path

    def _closure(self, member):
        install_dir = os.path.join(self.project_root, *member.split("/"))
        return production_closure(self.project_root, install_dir, self.lockfile_path)

    def test_member_closure_holds_own_dependencies_and_shared_package(self):
        closure = self._closure("functions/fn-b")

        expected = {
            os.path.realpath(self.project_root),
            os.path.join(os.path.realpath(self.project_root), "functions", "fn-b"),
            os.path.join(os.path.realpath(self.project_root), "packages", "shared"),
            os.path.join(os.path.realpath(self.project_root), "node_modules", "axios"),
            os.path.join(os.path.realpath(self.project_root), "node_modules", "follow-redirects"),
            os.path.join(os.path.realpath(self.project_root), "packages", "shared", "node_modules", "lodash"),
        }
        self.assertEqual(set(closure), expected)

    def test_conflicting_version_resolves_to_the_nested_copy_not_the_hoisted_one(self):
        # the shared package pins lodash 4.17.15 (nested); fn-b itself never depends on lodash,
        # so the hoisted node_modules/lodash (4.17.20) must NOT appear in fn-b's closure -
        # resolving the shared package's dependencies from its link path would find it
        closure = self._closure("functions/fn-b")

        hoisted = os.path.join(os.path.realpath(self.project_root), "node_modules", "lodash")
        nested = os.path.join(os.path.realpath(self.project_root), "packages", "shared", "node_modules", "lodash")
        self.assertNotIn(hoisted, closure)
        self.assertIn(nested, closure)

    def test_member_depending_on_the_hoisted_version_gets_both_copies(self):
        # fn-a resolves lodash^4.17.20 to the hoisted copy, and its shared dependency still
        # carries the nested 4.17.15
        closure = self._closure("functions/fn-a")

        self.assertIn(os.path.join(os.path.realpath(self.project_root), "node_modules", "lodash"), closure)
        self.assertIn(
            os.path.join(os.path.realpath(self.project_root), "packages", "shared", "node_modules", "lodash"), closure
        )

    def test_dev_dependencies_are_left_out(self):
        self.lock["packages"]["functions/fn-a"]["devDependencies"] = {"eslint": "^9.0.0"}
        self.lockfile_path = self._write_lockfile(self.lock)

        closure = self._closure("functions/fn-a")

        self.assertNotIn(os.path.join(os.path.realpath(self.project_root), "node_modules", "eslint"), closure)

    def test_entry_not_on_disk_is_left_out(self):
        shutil.rmtree(os.path.join(self.project_root, "node_modules", "follow-redirects"))

        closure = self._closure("functions/fn-b")

        self.assertNotIn(os.path.join(os.path.realpath(self.project_root), "node_modules", "follow-redirects"), closure)

    def test_version_1_lockfile_answers_none(self):
        self.lock["lockfileVersion"] = 1
        del self.lock["packages"]
        self.lockfile_path = self._write_lockfile(self.lock)

        self.assertIsNone(self._closure("functions/fn-a"))

    def test_install_dir_the_lockfile_does_not_describe_answers_none(self):
        os.makedirs(os.path.join(self.project_root, "functions", "fn-unknown"))

        self.assertIsNone(self._closure("functions/fn-unknown"))

    def test_unreadable_lockfile_answers_none(self):
        with open(self.lockfile_path, "w") as handle:
            handle.write("not json")

        self.assertIsNone(self._closure("functions/fn-a"))
