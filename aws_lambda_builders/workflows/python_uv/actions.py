"""
Action to resolve Python dependencies using UV
"""

import logging
import os
import shutil
from typing import Optional

from aws_lambda_builders.actions import ActionFailedError, BaseAction, CopySourceAction, Purpose
from aws_lambda_builders.architecture import X86_64
from aws_lambda_builders.utils import copytree

from .exceptions import LockFileError, MissingUvError, UvBuildError, UvInstallationError
from .packager import PythonUvDependencyBuilder, SubprocessUv, UvRunner, installed_source_copies, project_name
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

    def __init__(
        self,
        artifacts_dir,
        scratch_dir,
        manifest_path,
        runtime,
        architecture=X86_64,
        config=None,
        source_dir=None,
        excludes=(),
    ):
        self.artifacts_dir = artifacts_dir
        self.scratch_dir = scratch_dir
        self.manifest_path = manifest_path
        self.runtime = runtime
        self.architecture = architecture
        self.config = config or UvConfig()
        self.source_dir = source_dir
        self.excludes = excludes
        # Paths under source_dir that the install already put into artifacts_dir (see execute)
        self.source_copies = set()

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

        project_dir = os.path.dirname(self.manifest_path)
        name = project_name(project_dir)
        if self.source_dir and name and os.path.abspath(project_dir) == os.path.abspath(self.source_dir):
            self.source_copies = installed_source_copies(self.source_dir, self.artifacts_dir, name, self.excludes)
        else:
            LOG.debug("Copying the whole source dir: the project is not %s, or has no name", self.source_dir)


class PythonUvCopySourceAction(CopySourceAction):
    """CopySourceAction that leaves out the source of the installed project (see PythonUvInstallProjectAction)."""

    def __init__(self, source_dir, dest_dir, excludes, install_action):
        super().__init__(source_dir, dest_dir, excludes=excludes)
        self._install_action = install_action

    def execute(self):
        skip = self._install_action.source_copies
        patterns = shutil.ignore_patterns(*self.excludes)

        def ignore(directory, names):
            return patterns(directory, names) | {name for name in names if os.path.join(directory, name) in skip}

        copytree(self.source_dir, self.dest_dir, ignore=ignore, maintain_symlinks=self.maintain_symlinks)
