from unittest import TestCase
from unittest.mock import patch

from aws_lambda_builders.actions import ActionFailedError
from aws_lambda_builders.architecture import ARM64
from aws_lambda_builders.workflows.python_uv.actions import PythonUvInstallProjectAction
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
