import os
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from aws_lambda_builders.actions import ActionFailedError
from aws_lambda_builders.architecture import ARM64
from aws_lambda_builders.workflows.python_uv.actions import PythonUvCopySourceAction, PythonUvInstallProjectAction
from aws_lambda_builders.workflows.python_uv.exceptions import UvInstallationError
from aws_lambda_builders.workflows.python_uv.utils import UvConfig


class TestPythonUvInstallProjectAction(TestCase):
    @patch("aws_lambda_builders.workflows.python_uv.actions._dependency_builder")
    def test_installs_project_into_artifacts(self, builder_factory):
        config = UvConfig(install_project=True)
        PythonUvInstallProjectAction("artifacts", "scratch", "pyproject.toml", "python3.12", ARM64, config).execute()

        builder_factory.return_value.install_project.assert_called_once_with(
            manifest_path="pyproject.toml",
            target_dir="artifacts",
            scratch_dir="scratch",
            architecture=ARM64,
            config=config,
        )

    @patch("aws_lambda_builders.workflows.python_uv.actions._dependency_builder")
    def test_uv_failure_fails_the_action(self, builder_factory):
        builder_factory.return_value.install_project.side_effect = UvInstallationError(reason="boom")
        with self.assertRaises(ActionFailedError) as ctx:
            PythonUvInstallProjectAction("artifacts", "scratch", "pyproject.toml", "python3.12").execute()
        self.assertIn("boom", str(ctx.exception))


class TestPythonUvCopySourceAction(TestCase):
    def test_leaves_out_installed_source_copies_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, dest = os.path.join(tmp, "source"), os.path.join(tmp, "dest")
            for relative in ("app.py", "src/pkg/__init__.py", "src/keep.txt", "skip.pyc"):
                os.makedirs(os.path.dirname(os.path.join(source, relative)), exist_ok=True)
                open(os.path.join(source, relative), "w").close()
            install = Mock(source_copies={os.path.join(source, "src", "pkg")})

            PythonUvCopySourceAction(source, dest, ("*.pyc",), install).execute()

            copied = sorted(os.path.relpath(os.path.join(d, f), dest) for d, _, fs in os.walk(dest) for f in fs)
            self.assertEqual(copied, ["app.py", os.path.join("src", "keep.txt")])


class TestPythonUvInstallProjectActionSourceCopies(TestCase):
    @patch("aws_lambda_builders.workflows.python_uv.actions.installed_source_copies")
    @patch("aws_lambda_builders.workflows.python_uv.actions._dependency_builder")
    def test_source_copies_need_the_project_to_be_the_source_dir(self, _builder, find_copies):
        find_copies.return_value = {"/src/app/src/pkg"}
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "pyproject.toml"), "w") as f:
                f.write('[project]\nname = "app"\n')
            manifest = os.path.join(tmp, "pyproject.toml")

            same = PythonUvInstallProjectAction("art", "scr", manifest, "python3.12", source_dir=tmp, excludes=("x",))
            same.execute()
            elsewhere = PythonUvInstallProjectAction("art", "scr", manifest, "python3.12", source_dir="/other")
            elsewhere.execute()

        find_copies.assert_called_once_with(tmp, "art", "app", ("x",))
        self.assertEqual(same.source_copies, {"/src/app/src/pkg"})
        self.assertEqual(elsewhere.source_copies, set())
