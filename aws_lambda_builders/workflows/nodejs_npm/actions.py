"""
Action to resolve NodeJS dependencies using NPM
"""

import logging
import os
from typing import Optional

from aws_lambda_builders import utils
from aws_lambda_builders.actions import ActionFailedError, BaseAction, Purpose
from aws_lambda_builders.utils import extract_tarfile
from aws_lambda_builders.workflows.nodejs_npm.lockfile_closure import production_closure
from aws_lambda_builders.workflows.nodejs_npm.npm import NpmExecutionError, SubprocessNpm

LOG = logging.getLogger(__name__)


class NodejsNpmPackAction(BaseAction):
    """
    A Lambda Builder Action that packages a Node.js package using NPM to extract the source and remove test resources
    """

    NAME = "NpmPack"
    DESCRIPTION = "Packaging source using NPM"
    PURPOSE = Purpose.COPY_SOURCE

    def __init__(self, artifacts_dir, scratch_dir, manifest_path, osutils, subprocess_npm):
        """
        :type artifacts_dir: str
        :param artifacts_dir: an existing (writable) directory where to store the output.
            Note that the actual result will be in the 'package' subdirectory here.

        :type scratch_dir: str
        :param scratch_dir: an existing (writable) directory for temporary files

        :type manifest_path: str
        :param manifest_path: path to package.json of an NPM project with the source to pack

        :type osutils: aws_lambda_builders.workflows.nodejs_npm.utils.OSUtils
        :param osutils: An instance of OS Utilities for file manipulation

        :type subprocess_npm: aws_lambda_builders.workflows.nodejs_npm.npm.SubprocessNpm
        :param subprocess_npm: An instance of the NPM process wrapper
        """
        super(NodejsNpmPackAction, self).__init__()
        self.artifacts_dir = artifacts_dir
        self.manifest_path = manifest_path
        self.scratch_dir = scratch_dir
        self.osutils = osutils
        self.subprocess_npm = subprocess_npm

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when NPM packaging fails
        """
        try:
            package_path = "file:{}".format(self.osutils.abspath(self.osutils.dirname(self.manifest_path)))

            LOG.debug("NODEJS packaging %s to %s", package_path, self.scratch_dir)

            tarfile_name = self.subprocess_npm.run(["pack", "-q", package_path], cwd=self.scratch_dir).splitlines()[-1]

            LOG.debug("NODEJS packed to %s", tarfile_name)

            tarfile_path = self.osutils.joinpath(self.scratch_dir, tarfile_name)

            LOG.debug("NODEJS extracting to %s", self.artifacts_dir)

            extract_tarfile(tarfile_path, self.artifacts_dir)

        except NpmExecutionError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmInstallOrUpdateBaseAction(BaseAction):
    """
    A base Lambda Builder Action that is used for installs or updating NPM project dependencies
    """

    PURPOSE = Purpose.RESOLVE_DEPENDENCIES

    def __init__(self, install_dir: str, subprocess_npm: SubprocessNpm):
        """
        Parameters
        ----------
        install_dir : str
            Dependencies will be installed in this directory.
        subprocess_npm : SubprocessNpm
            An instance of the NPM process wrapper
        """

        super().__init__()
        self.install_dir = install_dir
        self.subprocess_npm = subprocess_npm


class NodejsNpmInstallAction(NodejsNpmInstallOrUpdateBaseAction):
    """
    A Lambda Builder Action that installs NPM project dependencies
    """

    NAME = "NpmInstall"
    DESCRIPTION = "Installing dependencies from NPM"

    def __init__(
        self,
        install_dir: str,
        subprocess_npm: SubprocessNpm,
        install_links: Optional[bool] = False,
        omit_dev: bool = True,
    ):
        """
        Parameters
        ----------
        install_dir : str
            Dependencies will be installed in this directory.
        subprocess_npm : SubprocessNpm
            An instance of the NPM process wrapper
        install_links : Optional[bool]
            Uses the --install-links npm option if True, by default False. Required when installing into the
            source directory, so that local file dependencies are installed as regular dependencies.
        omit_dev : bool
            Passes --omit=dev if True, by default True. False is for installing a whole workspace root,
            whose own dev dependencies (build tools like esbuild) must survive the install; production
            filtering of the artifacts does not rely on it there.
        """

        super().__init__(install_dir=install_dir, subprocess_npm=subprocess_npm)
        self.install_links = install_links
        self.omit_dev = omit_dev

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when NPM execution fails
        """
        try:
            LOG.debug("NODEJS installing dependencies in: %s", self.install_dir)

            command = ["install", "-q", "--no-audit", "--no-save"]
            if self.omit_dev:
                command.append("--omit=dev")
            if self.install_links:
                command.append("--install-links")
            self.subprocess_npm.run(command, cwd=self.install_dir)

        except NpmExecutionError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmUpdateAction(NodejsNpmInstallOrUpdateBaseAction):
    """
    A Lambda Builder Action that installs NPM project dependencies, ignoring any lockfile.

    Only used when building in source for a project that has no lockfile: `--no-package-lock` means
    dependency versions are resolved afresh on every build, so a project that does have a lockfile
    is installed with NodejsNpmInstallAction instead, to keep builds reproducible.
    """

    NAME = "NpmUpdate"
    DESCRIPTION = "Updating dependencies from NPM"

    def __init__(self, install_dir: str, subprocess_npm: SubprocessNpm, omit_dev: bool = True):
        """
        Parameters
        ----------
        install_dir : str
            Dependencies will be installed in this directory.
        subprocess_npm : SubprocessNpm
            An instance of the NPM process wrapper
        omit_dev : bool
            Passes --omit=dev if True, by default True. False is for installing a whole workspace root,
            whose own dev dependencies must survive the install.
        """

        super().__init__(install_dir=install_dir, subprocess_npm=subprocess_npm)
        self.omit_dev = omit_dev

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when NPM execution fails
        """
        try:
            LOG.debug("NODEJS updating dependencies in: %s", self.install_dir)

            command = ["update", "--no-audit", "--no-save"]
            if self.omit_dev:
                command.append("--omit=dev")
            command += [
                "--no-package-lock",
                "--install-links",
            ]
            self.subprocess_npm.run(command, cwd=self.install_dir)

        except NpmExecutionError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmCIAction(BaseAction):
    """
    A Lambda Builder Action that installs NPM project dependencies
    using the CI method - which is faster and better reproducible
    for CI environments, but requires a lockfile (package-lock.json
    or npm-shrinkwrap.json)
    """

    NAME = "NpmCI"
    DESCRIPTION = "Installing dependencies from NPM using the CI method"
    PURPOSE = Purpose.RESOLVE_DEPENDENCIES

    def __init__(self, install_dir: str, subprocess_npm: SubprocessNpm, install_links: Optional[bool] = False):
        """
        Parameters
        ----------
        install_dir : str
            Dependencies will be installed in this directory.
        subprocess_npm : SubprocessNpm
            An instance of the NPM process wrapper
        install_links : Optional[bool]
            Uses the --install-links npm option if True, by default False
        """

        super(NodejsNpmCIAction, self).__init__()
        self.install_dir = install_dir
        self.subprocess_npm = subprocess_npm
        self.install_links = install_links

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when NPM execution fails
        """

        try:
            LOG.debug("NODEJS installing ci in: %s", self.install_dir)

            command = ["ci"]
            if self.install_links:
                command.append("--install-links")

            self.subprocess_npm.run(command, cwd=self.install_dir)

        except NpmExecutionError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmrcAndLockfileCopyAction(BaseAction):
    """
    A Lambda Builder Action that copies lockfile and NPM config file .npmrc
    """

    NAME = "CopyNpmrcAndLockfile"
    DESCRIPTION = "Copying configuration from .npmrc and dependencies from lockfile/shrinkwrap"
    PURPOSE = Purpose.COPY_SOURCE

    def __init__(self, artifacts_dir, source_dir, osutils):
        """
        :type artifacts_dir: str
        :param artifacts_dir: an existing (writable) directory with project source files.
            Dependencies will be installed in this directory.

        :type source_dir: str
        :param source_dir: directory containing project source files.

        :type osutils: aws_lambda_builders.workflows.nodejs_npm.utils.OSUtils
        :param osutils: An instance of OS Utilities for file manipulation
        """

        super(NodejsNpmrcAndLockfileCopyAction, self).__init__()
        self.artifacts_dir = artifacts_dir
        self.source_dir = source_dir
        self.osutils = osutils

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when copying fails
        """

        try:
            for filename in [".npmrc", "package-lock.json", "npm-shrinkwrap.json"]:
                file_path = self.osutils.joinpath(self.source_dir, filename)
                if self.osutils.file_exists(file_path):
                    LOG.debug("%s copying in: %s", filename, self.artifacts_dir)
                    self.osutils.copy_file(file_path, self.artifacts_dir)

        except OSError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmrcCleanUpAction(BaseAction):
    """
    A Lambda Builder Action that cleans NPM config file .npmrc
    """

    NAME = "CleanUpNpmrc"
    DESCRIPTION = "Cleans artifacts dir"
    PURPOSE = Purpose.COPY_SOURCE

    def __init__(self, artifacts_dir, osutils):
        """
        :type artifacts_dir: str
        :param artifacts_dir: an existing (writable) directory with project source files.
            Dependencies will be installed in this directory.

        :type osutils: aws_lambda_builders.workflows.nodejs_npm.utils.OSUtils
        :param osutils: An instance of OS Utilities for file manipulation
        """

        super(NodejsNpmrcCleanUpAction, self).__init__()
        self.artifacts_dir = artifacts_dir
        self.osutils = osutils

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when deleting .npmrc fails
        """

        try:
            npmrc_path = self.osutils.joinpath(self.artifacts_dir, ".npmrc")
            if self.osutils.file_exists(npmrc_path):
                LOG.debug(".npmrc cleanup in: %s", self.artifacts_dir)
                self.osutils.remove_file(npmrc_path)

        except OSError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmLockFileCleanUpAction(BaseAction):
    """
    A Lambda Builder Action that cleans up garbage lockfile left by 7 in node_modules
    """

    NAME = "LockfileCleanUp"
    DESCRIPTION = "Cleans garbage lockfiles dir"
    PURPOSE = Purpose.COPY_SOURCE

    def __init__(self, artifacts_dir, osutils):
        """
        :type artifacts_dir: str
        :param artifacts_dir: an existing (writable) directory with project source files.
            Dependencies will be installed in this directory.

        :type osutils: aws_lambda_builders.workflows.nodejs_npm.utils.OSUtils
        :param osutils: An instance of OS Utilities for file manipulation
        """

        super(NodejsNpmLockFileCleanUpAction, self).__init__()
        self.artifacts_dir = artifacts_dir
        self.osutils = osutils

    def execute(self):
        """
        Runs the action.

        :raises lambda_builders.actions.ActionFailedError: when deleting the lockfile fails
        """

        try:
            npmrc_path = self.osutils.joinpath(self.artifacts_dir, "node_modules", ".package-lock.json")
            if self.osutils.file_exists(npmrc_path):
                LOG.debug(".package-lock cleanup in: %s", self.artifacts_dir)
                self.osutils.remove_file(npmrc_path)

        except OSError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmTestAction(NodejsNpmInstallOrUpdateBaseAction):
    """
    A Lambda Builder Action that runs tests in NPM project
    """

    NAME = "NpmTest"
    DESCRIPTION = "Running tests from NPM"

    def execute(self):
        """
        Runs the action if environment variable `SAM_NPM_RUN_TEST_WITH_BUILD` is `true`.

        :raises lambda_builders.actions.ActionFailedError: when NPM execution fails
        """
        try:
            is_run_test_with_build = os.getenv("SAM_NPM_RUN_TEST_WITH_BUILD", "False")
            if is_run_test_with_build == "true":
                LOG.debug("NODEJS running tests in: %s", self.install_dir)

                command = ["test", "--if-present"]
                self.subprocess_npm.run(command, cwd=self.install_dir)
            else:
                LOG.debug("NODEJS skipping tests")
                LOG.debug("Add env variable 'SAM_NPM_RUN_TEST_WITH_BUILD=true' to run tests with build")

        except NpmExecutionError as ex:
            raise ActionFailedError(str(ex))


class NodejsNpmLinkDependencyClosureAction(BaseAction):
    """
    A Lambda Builder Action that links only this function's own dependencies into the artifacts directory.

    Used when npm installed somewhere other than the function's directory, which is what npm does for a
    workspaces monorepo: it hoists every workspace package's dependencies into one node_modules at the
    monorepo root. Linking that whole directory would ship every sibling function's dependencies too, so
    this asks npm which packages this function actually resolves and links those under their own names.
    """

    NAME = "NpmLinkDependencyClosure"
    DESCRIPTION = "Linking this function's dependencies into the artifacts directory"
    PURPOSE = Purpose.LINK_SOURCE

    def __init__(self, install_dir, project_root, artifacts_dir, subprocess_npm, osutils, lockfile_path=None):
        """
        Parameters
        ----------
        install_dir : str
            the directory npm ran in, whose project's closure is wanted
        project_root : str
            the directory npm installed into, linked whole if the closure cannot be resolved
        artifacts_dir : str
            an existing (writable) directory where node_modules is assembled
        subprocess_npm : aws_lambda_builders.workflows.nodejs_npm.npm.SubprocessNpm
            An instance of the NPM process wrapper
        osutils : aws_lambda_builders.workflows.nodejs_npm.utils.OSUtils
            An instance of OS Utilities for file manipulation
        """
        super(NodejsNpmLinkDependencyClosureAction, self).__init__()
        self._install_dir = install_dir
        self._project_root = project_root
        self._artifacts_dir = artifacts_dir
        self._subprocess_npm = subprocess_npm
        self._osutils = osutils
        self._lockfile_path = lockfile_path

    def execute(self):
        # the lockfile already describes the resolved tree, so read it rather than paying an npm
        # process per function; it answers None for a version 1 lockfile, which has no path map
        closure = None
        if self._lockfile_path:
            closure = production_closure(self._project_root, self._install_dir, self._lockfile_path)
        if closure is None:
            closure = self._subprocess_npm.resolve_dependency_closure(self._install_dir)
        destination = os.path.join(self._artifacts_dir, "node_modules")

        if closure is None:
            # no answer from npm is no basis for leaving anything out, and an over-complete node_modules
            # still runs; link the whole installed tree instead
            LOG.debug("NODEJS linking all of %s into the artifacts", self._project_root)
            utils.create_symlink_or_copy(os.path.join(self._project_root, "node_modules"), destination)
            return

        for package_dir in self._outermost_packages(closure):
            try:
                name = self._osutils.parse_json(os.path.join(package_dir, "package.json"))["name"]
            except (OSError, ValueError, KeyError) as ex:
                # a directory npm named but that carries no readable manifest cannot be placed under a
                # name, and guessing one from the path is how a package lands where node will not find it
                raise ActionFailedError(f"Cannot read the package name of {package_dir}: {ex}")

            link_path = os.path.join(destination, *name.split("/"))
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            utils.create_symlink_or_copy(package_dir, link_path)

    def _outermost_packages(self, closure):
        """
        Keep the packages that need their own entry in node_modules.

        npm reports the project itself, which is not one of its own dependencies, and it reports a nested
        copy of a package that a dependency pins to a different version. A nested copy must stay where it
        is - hoisting it would shadow the top-level version for every other caller - and it is already
        reachable through the dependency that contains it, so only the outermost paths are linked.
        """
        # Every comparison below goes through normcase, never the raw path. When the closure comes from
        # `npm ls` these paths are npm's spelling while project_root and install_dir are the build's, and
        # on Windows two spellings differing only in case name the same directory: an unmatched project
        # root would be linked as if it were one of its own dependencies, and an unrecognised nested copy
        # would be hoisted to the top level, shadowing the version every other caller resolves. The
        # original paths are what gets linked, so only the comparisons are normalised. No-op off Windows.
        paths = [os.path.realpath(path) for path in closure]
        excluded = {os.path.normcase(os.path.realpath(d)) for d in (self._project_root, self._install_dir)}
        candidates = [path for path in paths if os.path.normcase(path) not in excluded]

        def is_nested_in_another(path):
            key = os.path.normcase(path)
            return any(
                key != os.path.normcase(other) and key.startswith(os.path.normcase(other) + os.sep)
                for other in candidates
            )

        return [path for path in candidates if not is_nested_in_another(path)]
