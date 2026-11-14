"""
UV-based Python dependency packager for AWS Lambda
"""

import csv
import filecmp
import logging
import os
import re
import shutil
from typing import Dict, List, Optional, Set

from aws_lambda_builders.architecture import ARM64, X86_64

from .exceptions import LockFileError, MissingUvError, UvBuildError, UvInstallationError
from .utils import OSUtils, UvConfig

LOG = logging.getLogger(__name__)


def project_name(project_dir: str) -> Optional[str]:
    """The [project] name in project_dir/pyproject.toml, or None when it is missing or unreadable."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    try:
        with open(os.path.join(project_dir, "pyproject.toml"), "rb") as pyproject_file:
            project = tomllib.load(pyproject_file).get("project")
    except (OSError, ValueError):
        return None
    name = project.get("name") if isinstance(project, dict) else None
    return name if isinstance(name, str) and name else None


def installed_source_copies(source_dir: str, target_dir: str, name: str, excludes=()) -> Set[str]:
    """Source paths that the project install already put into target_dir, byte for byte.

    In a src layout, copying the source dir would ship the package a second time as src/<pkg>,
    which Lambda never imports. Only a package or module one directory below source_dir, found in
    exactly one place, whose every (non-excluded) file was installed unchanged, is returned, plus
    its parent dir when nothing else is left in it. Anything else (flat layout, files the wheel
    left out, symlinks, no RECORD) keeps the full copy.
    """
    ignore = shutil.ignore_patterns(*excludes)
    normalized = re.sub(r"[-_.]+", "_", name).lower()
    records = [
        os.path.join(target_dir, entry, "RECORD")
        for entry in os.listdir(target_dir)
        if entry.endswith(".dist-info") and re.sub(r"[-_.]+", "_", entry.split("-")[0]).lower() == normalized
    ]
    if len(records) != 1:
        LOG.debug("Copying the whole source dir: found %d dist-info dirs for %s", len(records), name)
        return set()
    try:
        with open(records[0], newline="", encoding="utf-8") as record_file:
            rows = list(csv.reader(record_file))
    except OSError as ex:
        LOG.debug("Copying the whole source dir: cannot read %s: %s", records[0], ex)
        return set()

    installed: Dict[str, Set[str]] = {}
    for row in rows:
        parts = row[0].split("/") if row and row[0] else []
        if not parts or parts[0] in ("", "..") or ".." in parts or "__pycache__" in parts:
            continue
        if parts[0].endswith((".dist-info", ".data")):
            continue
        installed.setdefault(parts[0], set()).add(row[0])

    copies = set()
    for top, paths in installed.items():
        if os.path.lexists(os.path.join(source_dir, top)):
            continue  # flat layout: keep copying it, as without uv_install_project
        candidates = [
            os.path.join(source_dir, entry, top)
            for entry in sorted(os.listdir(source_dir))
            if entry not in ignore(source_dir, [entry])
            and os.path.isdir(os.path.join(source_dir, entry))
            and not os.path.islink(os.path.join(source_dir, entry))
            and os.path.lexists(os.path.join(source_dir, entry, top))
        ]
        if len(candidates) == 1 and _installed_unchanged(candidates[0], paths, target_dir, ignore):
            copies.add(candidates[0])
        else:
            LOG.debug("Copying the source of %s: cannot match it to one installed copy", top)

    for parent in {os.path.dirname(path) for path in copies}:
        names = os.listdir(parent)
        ignored = ignore(parent, names)
        if all(os.path.join(parent, entry) in copies for entry in names if entry not in ignored):
            copies.add(parent)
    return copies


def _installed_unchanged(source_path: str, installed_paths: Set[str], target_dir: str, ignore) -> bool:
    """Whether every source file under source_path was installed, unchanged, as one of installed_paths."""
    if os.path.islink(source_path):
        return False
    files = []
    if os.path.isfile(source_path):
        files.append(source_path)
    else:
        for directory, dirnames, filenames in os.walk(source_path):
            ignored = ignore(directory, dirnames + filenames)
            kept = [entry for entry in dirnames + filenames if entry not in ignored]
            if any(os.path.islink(os.path.join(directory, entry)) for entry in kept):
                return False
            dirnames[:] = [entry for entry in dirnames if entry not in ignored]
            files.extend(os.path.join(directory, entry) for entry in filenames if entry not in ignored)
    root = os.path.dirname(source_path)
    for file in files:
        relative = os.path.relpath(file, root).replace(os.sep, "/")
        if relative not in installed_paths or not filecmp.cmp(file, os.path.join(target_dir, relative), False):
            return False
    return True


class SubprocessUv:
    """Low-level interface for executing UV commands via subprocess."""

    def __init__(self, osutils: Optional[OSUtils] = None):
        if osutils is None:
            osutils = OSUtils()
        self._osutils = osutils
        self._uv_executable = self._find_uv_executable()
        self._warnings_seen: Set[str] = set()

    def _find_uv_executable(self) -> str:
        """Find UV executable in PATH."""
        uv_path = self._osutils.which("uv")
        if not uv_path:
            raise MissingUvError()
        return uv_path

    @property
    def uv_executable(self) -> str:
        """Get UV executable path."""
        return self._uv_executable

    def get_uv_version(self) -> Optional[str]:
        """
        Get UV version from the executable.

        Returns:
            UV version string or None if unable to determine
        """
        try:
            rc, stdout, stderr = self._osutils.run_subprocess([self._uv_executable, "--version"])
            if rc == 0 and stdout:
                # UV version output format: "uv 0.1.0" -> ["uv", "0.1.0"]
                parts = stdout.strip().split()
                version_index = 1
                if len(parts) > version_index:
                    return parts[version_index]
        except Exception:
            pass
        return None

    def run_uv_command(self, args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> tuple:
        """
        Execute UV command with given arguments.

        Args:
            args: UV command arguments
            cwd: Working directory
            env: Environment variables

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        cmd = [self._uv_executable] + args
        LOG.debug("Executing UV command: %s", " ".join(cmd))

        rc, stdout, stderr = self._osutils.run_subprocess(cmd, cwd=cwd, env=env)

        LOG.debug("UV command return code: %d", rc)
        LOG.debug("UV stdout: %s", stdout)
        LOG.debug("UV stderr: %s", stderr)
        self._log_warnings(stderr)

        return rc, stdout, stderr

    def _log_warnings(self, stderr: Optional[str]) -> None:
        """Log each `warning:` block uv prints, once per build; uv repeats some on every command."""
        blocks: List[List[str]] = []
        current = None
        for line in (stderr or "").splitlines():
            if line.startswith("warning:"):
                current = [line]
                blocks.append(current)
            elif current is not None and line[:1] in (" ", "\t"):
                current.append(line)  # uv indents a warning's continuation lines
            else:
                current = None
        for block in blocks:
            message = "\n".join(block)
            if message not in self._warnings_seen:
                self._warnings_seen.add(message)
                LOG.warning("uv %s", message)


class UvRunner:
    """High-level wrapper around UV operations."""

    def __init__(self, uv_subprocess: Optional[SubprocessUv] = None, osutils: Optional[OSUtils] = None):
        if osutils is None:
            osutils = OSUtils()
        if uv_subprocess is None:
            uv_subprocess = SubprocessUv(osutils)

        self._uv = uv_subprocess
        self._osutils = osutils

    @property
    def uv_version(self) -> Optional[str]:
        """Get UV version."""
        return self._uv.get_uv_version()

    def install_requirements(
        self,
        requirements_path: str,
        target_dir: str,
        config: Optional[UvConfig] = None,
        python_version: Optional[str] = None,
        platform: Optional[str] = None,
        architecture: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        """
        Install requirements using UV pip interface.

        No ``--cache-dir`` is passed unless the caller supplies one, so UV uses its own
        default cache location. That cache outlives a single build, letting UV reuse
        previously downloaded dependencies across functions and across builds. Pointing UV
        at a per-build directory instead would discard the cache every time, since the
        build's scratch directory is deleted when the build finishes.

        Args:
            requirements_path: Path to requirements.txt file
            target_dir: Directory to install dependencies
            config: UV configuration options
            python_version: Target Python version
            platform: Target platform
            architecture: Target architecture
        """
        # Resolve the requirements file before UV changes to the project or workspace directory
        self._pip_install(
            ["-r", os.path.abspath(requirements_path)], target_dir, config, python_version, platform, architecture, cwd
        )

    def install_project(
        self,
        project_dir: str,
        build_dir: str,
        target_dir: str,
        config: Optional[UvConfig] = None,
        python_version: Optional[str] = None,
        platform: Optional[str] = None,
        architecture: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        """Install the project itself, non-editable and without its dependencies.

        setuptools builds in the source tree, leaving build/ and *.egg-info there for
        CopySourceAction to ship. DIST_EXTRA_CONFIG moves both into ``build_dir``; other
        build backends ignore it. Only this one package is built here: setuptools packs
        everything under build_base into the wheel, so packages must not share one.

        shortcut: local path dependencies (e.g. workspace members) are still built in their
        own source trees by the dependency install; give each its own build_base if that matters.
        """
        os.makedirs(build_dir, exist_ok=True)
        config_path = os.path.join(build_dir, "setup.cfg")
        # setuptools reads this file with configparser interpolation, so a literal % must be %%
        escaped_dir = build_dir.replace("%", "%%")
        with open(config_path, "w", encoding="utf-8") as config_file:
            config_file.write(
                f"[build]\nbuild_base = {os.path.join(escaped_dir, 'build')}\n[egg_info]\negg_base = {escaped_dir}\n"
            )
        env = dict(self._osutils.original_environ(), DIST_EXTRA_CONFIG=config_path)
        self._pip_install(
            ["--no-deps", os.path.abspath(project_dir), *self._rebuild_args(project_dir)],
            target_dir,
            config,
            python_version,
            platform,
            architecture,
            cwd,
            env=env,
        )

    @staticmethod
    def _rebuild_args(project_dir: str) -> List[str]:
        """Make uv build the project afresh instead of reusing a wheel it cached from an earlier build.

        uv's documented cache keys for a local directory are pyproject.toml/setup.py/setup.cfg, not the code
        (uv 0.13 rebuilds anyway, but that is not a promise). --reinstall-package rebuilds only the project.
        --reinstall, used when pyproject.toml has no project name, also revalidates cached build
        dependencies such as setuptools, so it is only the fallback.
        """
        name = project_name(project_dir)
        return ["--reinstall-package", name] if name else ["--reinstall"]

    def _pip_install(
        self,
        sources: List[str],
        target_dir: str,
        config: Optional[UvConfig],
        python_version: Optional[str],
        platform: Optional[str],
        architecture: Optional[str],
        cwd: Optional[str],
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        if config is None:
            config = UvConfig()

        args = ["pip", "install"]
        args.extend(sources)

        # Resolve --target to an absolute path: UV runs from the project or workspace directory,
        # so a relative target (e.g. the incremental-build dependencies dir) would otherwise be
        # created under the UV's cwd instead of the build root.
        args.extend(["--target", os.path.abspath(target_dir)])

        # Add configuration arguments
        args.extend(config.to_uv_args())

        # Add platform-specific arguments
        if python_version:
            args.extend(["--python-version", python_version])

        if platform and architecture:
            # UV pip install uses --python-platform format
            # Map Lambda architectures to UV platform strings
            platform_mapping = {
                ("linux", X86_64): "x86_64-unknown-linux-gnu",
                ("linux", ARM64): "aarch64-unknown-linux-gnu",
            }

            platform_key = (platform, architecture)
            if platform_key in platform_mapping:
                python_platform = platform_mapping[platform_key]
                if python_version:
                    version_info = tuple(int(part) for part in python_version.split(".")[:2])
                    # Match Lambda's AL2/AL2023 manylinux targets, not the build host's glibc.
                    manylinux_version = "2_34" if version_info >= (3, 12) else "2_17"
                    target_arch = "aarch64" if architecture == ARM64 else "x86_64"
                    python_platform = f"{target_arch}-manylinux_{manylinux_version}"
                args.extend(["--python-platform", python_platform])

        # Execute UV pip install
        rc, stdout, stderr = self._uv.run_uv_command(args, cwd, env=env)

        if rc != 0:
            raise UvInstallationError(reason=f"UV pip install failed: {stderr}")

        LOG.debug("UV pip install completed successfully: %s", stdout)


class PythonUvDependencyBuilder:
    """High-level dependency builder that orchestrates UV operations."""

    def __init__(
        self, osutils: Optional[OSUtils] = None, runtime: Optional[str] = None, uv_runner: Optional[UvRunner] = None
    ):
        if osutils is None:
            osutils = OSUtils()
        if uv_runner is None:
            uv_runner = UvRunner(osutils=osutils)

        self._osutils = osutils
        self._uv_runner = uv_runner
        self.runtime = runtime

    def build_dependencies(
        self,
        artifacts_dir_path: str,
        scratch_dir_path: str,
        manifest_path: str,
        architecture: str = X86_64,
        config: Optional[UvConfig] = None,
    ) -> None:
        """
        Build Python dependencies using UV.

        Args:
            artifacts_dir_path: Directory to write dependencies
            scratch_dir_path: Temporary directory for build operations
            manifest_path: Path to dependency manifest file
            architecture: Target architecture (X86_64 or ARM64)
            config: UV configuration options
        """
        LOG.info("Building Python dependencies using UV")
        LOG.info("Manifest file: %s", manifest_path)
        LOG.info("Target architecture: %s", architecture)
        LOG.info("Using scratch directory: %s", scratch_dir_path)

        if config is None:
            config = UvConfig()

        # Determine Python version from runtime
        python_version = self._extract_python_version(self.runtime)

        # Determine manifest type and build accordingly
        manifest_name = os.path.basename(manifest_path)

        try:
            # Get the appropriate build handler based on manifest type
            # This dispatch system allows different build strategies:
            # - pyproject.toml: Uses uv export + uv pip install (for cross-platform support)
            # - requirements.txt: Uses uv pip install with platform targeting
            handler = self._get_manifest_handler(manifest_name)

            # Execute the selected build strategy
            # All paths ultimately use UvRunner.install_requirements() with --python-platform
            # for cross-platform Lambda builds (Linux x86_64 or ARM64)
            handler(manifest_path, artifacts_dir_path, scratch_dir_path, python_version, architecture, config)

        except Exception as e:
            LOG.error("Failed to build dependencies: %s", str(e))
            raise

    def install_project(
        self,
        manifest_path: str,
        target_dir: str,
        scratch_dir: str,
        architecture: str = X86_64,
        config: Optional[UvConfig] = None,
    ) -> None:
        """Install the project of manifest_path (a pyproject.toml) into target_dir, without its dependencies.

        This is separate from build_dependencies because the project is the function's own code:
        it must be rebuilt on every build, also when the dependencies are reused from a previous one.
        """
        project_dir = os.path.dirname(manifest_path)
        self._uv_runner.install_project(
            project_dir=project_dir,
            build_dir=os.path.abspath(os.path.join(scratch_dir, "project-build")),
            target_dir=target_dir,
            config=config,
            python_version=self._extract_python_version(self.runtime),
            platform="linux",
            architecture=architecture,
            cwd=self._resolve_workspace_dir(project_dir),
        )

    def _get_manifest_handler(self, manifest_name: str):
        """Get the appropriate handler function for a manifest file.

        Uses a dictionary-based dispatch pattern for extensibility.
        This allows easy addition of new manifest types in the future
        without modifying the core dispatch logic.
        """
        # Exact match handlers for specific manifest files
        exact_handlers = {
            "pyproject.toml": self._handle_pyproject_build,
        }

        # Check for exact match first
        if manifest_name in exact_handlers:
            return exact_handlers[manifest_name]

        # Check for requirements file pattern
        if self._is_requirements_file(manifest_name):
            return self._build_from_requirements

        # Generic unsupported file - covers uv.lock and everything else
        raise UvBuildError(reason=f"Unsupported manifest file: {manifest_name}")

    def _handle_pyproject_build(
        self,
        manifest_path: str,
        target_dir: str,
        scratch_dir: str,
        python_version: str,
        architecture: str,
        config: UvConfig,
    ) -> None:
        """
        Smart pyproject.toml handler that checks for uv.lock.

        If uv.lock exists, use lock-based build for more precise dependency resolution.
        Otherwise, use standard pyproject.toml build.
        """
        project_dir = os.path.dirname(manifest_path)
        workspace_dir = self._resolve_workspace_dir(project_dir)
        uv_lock_path = os.path.join(workspace_dir, "uv.lock")

        if os.path.exists(uv_lock_path):
            LOG.info("Found uv.lock at %s - using lock-based build for precise dependencies", uv_lock_path)
            # Use lock file for more precise builds
            self._build_from_lock_file(
                uv_lock_path,
                target_dir,
                scratch_dir,
                python_version,
                architecture,
                config,
                project_dir=project_dir,
                workspace_dir=workspace_dir,
            )
        else:
            # Standard pyproject.toml build
            self._build_from_pyproject(
                manifest_path,
                target_dir,
                scratch_dir,
                python_version,
                architecture,
                config,
                workspace_dir=workspace_dir,
            )

    def _resolve_workspace_dir(self, project_dir: str) -> str:
        """Find the workspace root, falling back for UV versions without this command."""
        version = self._uv_runner.uv_version
        # Only interpret plain release versions. Probe the command for unknown
        # or prerelease versions instead of assuming whether it is supported.
        if version and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            if tuple(int(part) for part in version.split(".")) < (0, 9, 9):
                self._log_workspace_fallback(project_dir)
                return project_dir

        rc, stdout, stderr = self._uv_runner._uv.run_uv_command(["workspace", "dir"], cwd=project_dir)
        workspace_dir = stdout.strip() if rc == 0 else ""
        if workspace_dir:
            return workspace_dir
        if rc != 0 and "unrecognized subcommand 'workspace'" in stderr:
            self._log_workspace_fallback(project_dir)
            return project_dir
        # Other failures do not establish that this is a standalone project. Stop
        # rather than risk missing a workspace lock and regenerating it, or installing
        # workspace-relative dependencies from the wrong directory. This deliberately
        # also stops standalone builds when discovery fails. A successful exit with
        # empty output is still a failure to discover the root, not evidence of no workspace.
        reason = stderr.strip() or f"uv workspace dir returned no workspace path (exit code {rc})"
        raise UvBuildError(reason=f"Could not determine the UV workspace root for {project_dir}: {reason}")

    def _log_workspace_fallback(self, project_dir: str) -> None:
        """Use workspace configuration only as a warning hint, not as a root or membership decision."""
        LOG.debug(
            "This UV build does not support `uv workspace dir` (requires uv >= 0.9.9); "
            "falling back to project directory %s.",
            project_dir,
        )
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            try:
                import tomli as tomllib
            except ModuleNotFoundError:
                LOG.debug("Skipping workspace configuration hint: no TOML parser is available.")
                return

        directory = os.path.abspath(project_dir)
        while True:
            manifest = os.path.join(directory, "pyproject.toml")
            try:
                with open(manifest, "rb") as file:
                    config = tomllib.load(file)
                # Ancestor manifests may contain valid TOML with unrelated value types.
                # Only an actual table is a workspace hint; diagnostics must not fail the build.
                tool_config = config.get("tool")
                uv_config = tool_config.get("uv") if isinstance(tool_config, dict) else None
                has_uv_workspace_table = isinstance(uv_config, dict) and isinstance(uv_config.get("workspace"), dict)
                if has_uv_workspace_table:
                    LOG.warning(
                        "UV does not support `uv workspace dir` (requires uv >= 0.9.9), and workspace "
                        "configuration was found in %s. Falling back to project directory %s. "
                        "If this project is a member of that workspace, upgrade uv. "
                        "Workspace members and exclusions have not been evaluated.",
                        manifest,
                        project_dir,
                    )
                    return
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                LOG.debug("Could not inspect workspace configuration in %s: %s", manifest, error)
            parent = os.path.dirname(directory)
            if parent == directory:
                return
            directory = parent

    def _is_requirements_file(self, filename: str) -> bool:
        """
        Check if a filename represents a valid requirements file.

        Follows Python ecosystem conventions:
        - requirements.txt (standard)
        - requirements-*.txt (environment-specific: dev, test, prod, etc.)
        """
        if filename == "requirements.txt":
            return True

        # Allow environment-specific requirements files like requirements-dev.txt
        # Must have at least one character after the dash and before .txt
        if (
            filename.startswith("requirements-")
            and filename.endswith(".txt")
            and len(filename) > len("requirements-.txt")
        ):
            return True

        return False

    @staticmethod
    def _lock_export_options(lock_path: str) -> List[str]:
        """Reuse the resolver settings that produced the lock when checking it for export."""
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib

        try:
            with open(lock_path, "rb") as lock_file:
                options = tomllib.load(lock_file).get("options", {})
        except FileNotFoundError:
            # Leave missing-lock diagnostics to UV.
            return []

        # Relative cutoffs also store a placeholder date for backwards compatibility.
        if "exclude-newer-span" in options:
            options["exclude-newer"] = options["exclude-newer-span"]

        args = []
        for option, flag in (
            ("resolution-mode", "--resolution"),
            ("prerelease-mode", "--prerelease"),
            ("fork-strategy", "--fork-strategy"),
            ("exclude-newer", "--exclude-newer"),
        ):
            if option in options:
                args.extend([flag, options[option]])
        for package, cutoff in options.get("exclude-newer-package", {}).items():
            value = cutoff
            if isinstance(cutoff, dict):
                value = cutoff["span"] if "span" in cutoff else cutoff["timestamp"]
            args.extend(["--exclude-newer-package", f"{package}={value}"])
        return args

    @staticmethod
    def _check_prune_names(lock_path: str, prune: List[str]) -> None:
        """Fail on prune names missing from the lock; `uv export --prune` ignores them silently."""
        if not prune:
            return
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib

        try:
            with open(lock_path, "rb") as lock_file:
                packages = tomllib.load(lock_file).get("package", [])
        except FileNotFoundError:
            # Leave missing-lock diagnostics to UV.
            return

        locked = {package.get("name") for package in packages if isinstance(package, dict)}
        missing = [name for name in prune if re.sub(r"[-_.]+", "-", name).lower() not in locked]
        if missing:
            raise LockFileError(reason=f"Build option uv_prune names packages not in {lock_path}: {', '.join(missing)}")

    def _build_from_lock_file(
        self,
        lock_path: str,
        target_dir: str,
        scratch_dir: str,
        python_version: str,
        architecture: str,
        config: UvConfig,
        project_dir: Optional[str] = None,
        workspace_dir: Optional[str] = None,
    ) -> None:
        """Build dependencies from uv.lock file.

        Uses uv export to convert lock file to requirements.txt, then installs
        with uv pip install which supports cross-platform builds (--python-platform).
        This is necessary because uv sync doesn't support platform targeting.
        """
        LOG.info("Building from UV lock file")

        try:
            if project_dir is None:
                project_dir = os.path.dirname(lock_path)
            self._check_prune_names(lock_path, config.prune)

            # Export lock file to requirements.txt for platform-specific install
            # Export and install may run from different directories in a workspace
            temp_requirements = os.path.abspath(os.path.join(scratch_dir, "lock_requirements.txt"))
            export_args = [
                "export",
                "--locked",  # Reject stale locks rather than rewriting them or omitting new dependencies
                "--format",
                "requirements.txt",
                "--no-default-groups",  # Exclude PEP 735 default groups (e.g. dev/test) from Lambda zips
                # Install package bodies instead of editable .pth links, which break in Lambda zips.
                "--no-editable",
                "--output-file",
                temp_requirements,
                # We want to specify the version because `uv export` might default to using a different one
                # This is important for dependencies that use different versions depending on python version
                "--python",
                python_version,
            ]
            # Only dependencies; uv_install_project installs the project itself (see install_project)
            export_args.extend(config.to_export_args())
            export_args.extend(self._lock_export_options(lock_path))

            rc, stdout, stderr = self._uv_runner._uv.run_uv_command(export_args, cwd=project_dir)
            if rc != 0:
                raise LockFileError(reason=f"Failed to export lock file: {stderr}")

            # Export selects the member's dependencies, but exported local paths
            # are relative to the workspace root.
            if workspace_dir is None:
                workspace_dir = self._resolve_workspace_dir(project_dir)

            # Install with platform targeting
            self._uv_runner.install_requirements(
                requirements_path=temp_requirements,
                target_dir=target_dir,
                config=config,
                python_version=python_version,
                platform="linux",
                cwd=workspace_dir,
                architecture=architecture,
            )
        except LockFileError:
            raise
        except Exception as e:
            raise LockFileError(reason=str(e))

    def _build_from_pyproject(
        self,
        pyproject_path: str,
        target_dir: str,
        scratch_dir: str,
        python_version: str,
        architecture: str,
        config: UvConfig,
        workspace_dir: Optional[str] = None,
    ) -> None:
        """Build dependencies from pyproject.toml file using UV's native workflow."""
        LOG.info("Building from pyproject.toml using UV lock and export")

        try:
            # Generate lock file from pyproject.toml
            LOG.debug("Creating lock file from pyproject.toml")
            lock_args = ["lock", "--no-progress"]
            if python_version:
                lock_args.extend(["--python", python_version])

            project_dir = os.path.dirname(pyproject_path)
            rc, stdout, stderr = self._uv_runner._uv.run_uv_command(lock_args, cwd=project_dir)
            if rc != 0:
                raise UvBuildError(reason=f"UV lock failed: {stderr}")

            # Reuse lock file build logic
            if workspace_dir is None:
                workspace_dir = self._resolve_workspace_dir(project_dir)
            lock_path = os.path.join(workspace_dir, "uv.lock")
            self._build_from_lock_file(
                lock_path,
                target_dir,
                scratch_dir,
                python_version,
                architecture,
                config,
                project_dir=project_dir,
                workspace_dir=workspace_dir,
            )

        except Exception as e:
            raise UvBuildError(reason=f"Failed to build from pyproject.toml: {str(e)}")

    def _build_from_requirements(
        self,
        requirements_path: str,
        target_dir: str,
        scratch_dir: str,
        python_version: str,
        architecture: str,
        config: UvConfig,
    ) -> None:
        """Build dependencies from requirements.txt file.

        ``scratch_dir`` is unused here; it is part of the signature shared by every manifest
        handler dispatched from :meth:`build_dependencies`.
        """
        LOG.info("Building from requirements file")

        try:
            # Resolve the manifest before running UV from its directory for local dependencies.
            requirements_path = os.path.abspath(requirements_path)
            self._uv_runner.install_requirements(
                requirements_path=requirements_path,
                target_dir=target_dir,
                config=config,
                python_version=python_version,
                platform="linux",
                architecture=architecture,
                cwd=os.path.dirname(requirements_path),
            )
        except Exception as e:
            raise UvBuildError(reason=f"Failed to build from requirements: {str(e)}")

    def _extract_python_version(self, runtime: Optional[str]) -> str:
        """Extract Python version from runtime string."""
        if not runtime:
            raise UvBuildError(reason="Runtime is required but was not provided")

        # Extract version from runtime like "python3.9" -> "3.9"
        if runtime.startswith("python"):
            return runtime.replace("python", "")

        return runtime
