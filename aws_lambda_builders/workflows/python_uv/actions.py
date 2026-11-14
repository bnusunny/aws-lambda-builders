"""
Action to resolve Python dependencies using UV
"""

import logging
from typing import Optional

from aws_lambda_builders.actions import ActionFailedError, BaseAction, Purpose
from aws_lambda_builders.architecture import X86_64

from .exceptions import LockFileError, MissingUvError, UvBuildError, UvInstallationError
from .packager import PythonUvDependencyBuilder, SubprocessUv, UvRunner
from .utils import OSUtils, UvConfig

LOG = logging.getLogger(__name__)


def _dependency_builder(osutils, runtime):
    uv_runner = UvRunner(uv_subprocess=SubprocessUv(osutils=osutils), osutils=osutils)
    return PythonUvDependencyBuilder(osutils=osutils, runtime=runtime, uv_runner=uv_runner)


class PythonUvBuildAction(BaseAction):
    """Action for building Python dependencies using UV."""

    NAME = "ResolveDependencies"
    DESCRIPTION = "Installing dependencies using UV"
    PURPOSE = Purpose.RESOLVE_DEPENDENCIES
    LANGUAGE = "python"

    def __init__(
        self,
        artifacts_dir,
        scratch_dir,
        manifest_path,
        runtime,
        dependencies_dir,
        binaries,
        architecture=X86_64,
        config: Optional[UvConfig] = None,
    ):
        self.artifacts_dir = artifacts_dir
        self.manifest_path = manifest_path
        self.scratch_dir = scratch_dir
        self.runtime = runtime
        self.dependencies_dir = dependencies_dir
        self.binaries = binaries
        self.architecture = architecture
        self.config = config or UvConfig()

        self._os_utils = OSUtils()

    def execute(self) -> None:
        """Execute the build action for Python UV workflows."""
        package_builder = _dependency_builder(self._os_utils, self.runtime)

        try:
            # Determine target directory
            target_artifact_dir = self.artifacts_dir
            if self.dependencies_dir:
                target_artifact_dir = self.dependencies_dir

            # Build dependencies
            package_builder.build_dependencies(
                artifacts_dir_path=target_artifact_dir,
                scratch_dir_path=self.scratch_dir,
                manifest_path=self.manifest_path,
                architecture=self.architecture,
                config=self.config,
            )
        except (LockFileError, MissingUvError, UvInstallationError, UvBuildError) as ex:
            raise ActionFailedError(str(ex))


class PythonUvInstallProjectAction(BaseAction):
    """Action for installing the project itself (uv_install_project) into the artifacts."""

    NAME = "InstallProject"
    DESCRIPTION = "Installing the project using UV"
    PURPOSE = Purpose.COMPILE_SOURCE
    LANGUAGE = "python"

    def __init__(self, artifacts_dir, scratch_dir, manifest_path, runtime, architecture=X86_64, config=None):
        self.artifacts_dir = artifacts_dir
        self.scratch_dir = scratch_dir
        self.manifest_path = manifest_path
        self.runtime = runtime
        self.architecture = architecture
        self.config = config or UvConfig()

        self._os_utils = OSUtils()

    def execute(self) -> None:
        try:
            _dependency_builder(self._os_utils, self.runtime).install_project(
                manifest_path=self.manifest_path,
                target_dir=self.artifacts_dir,
                scratch_dir=self.scratch_dir,
                architecture=self.architecture,
                config=self.config,
            )
        except (MissingUvError, UvInstallationError, UvBuildError) as ex:
            raise ActionFailedError(str(ex))
