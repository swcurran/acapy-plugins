#!/usr/bin/env python3
"""A script to run a local test of a specific version of acapy-agent against all
(or selected) plugins' integration tests. The script is designed for testing
ACA-Py release candidates to verify them before a new release.

The script pins each plugin's acapy-agent dependency to the specified version,
runs the integration tests, and then reverts any changes made to
pyproject.toml and poetry.lock files.

Usage: ./test_acapy_version.py <acapy-agent-version> [plugin ...]

Examples: ./test_acapy_version.py 1.7.0rc0
          ./test_acapy_version.py 1.7.0rc0 basicmessage_storage webvh

Requires: poetry, docker (with compose plugin). Run from the repo root.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Matches pr-integration-tests.yaml, which skips cheqd's integration tests.
SKIP_TESTS = {"cheqd"}

# plugin_globals holds shared pyproject.toml sections for repo_manager.py; it
# isn't an actual plugin (matches pr-integration-tests.yaml, which excludes it
# the same way).
EXCLUDED_PLUGINS = {"plugin_globals"}

ACAPY_DEP_RE = re.compile(r'(acapy-agent\s*=\s*\{\s*version\s*=\s*")[^"]+(")')


def discover_plugins() -> list[str]:
    """A plugin is any top-level directory whose pyproject.toml declares an
    acapy-agent dependency. Derived instead of hardcoded so new plugins are
    picked up automatically.
    """
    plugins = []
    for pyproject in sorted(REPO_ROOT.glob("*/pyproject.toml")):
        plugin = pyproject.parent.name
        if plugin in EXCLUDED_PLUGINS:
            continue
        if "acapy-agent" in pyproject.read_text():
            plugins.append(plugin)
    return plugins


def check_clean() -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", "*/pyproject.toml", "*/poetry.lock"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(
            "error: uncommitted changes already present in pyproject.toml/poetry.lock "
            "files. Commit or stash first.",
            file=sys.stderr,
        )
        print(result.stdout, file=sys.stderr)
        sys.exit(1)


def revert(touched_files: list[str]) -> None:
    print()
    print("== Reverting version bump changes ==")
    if touched_files:
        subprocess.run(["git", "checkout", "--", *touched_files], cwd=REPO_ROOT)
        print("Reverted:", " ".join(touched_files))
    else:
        print("(nothing to revert)")


def set_version(pyproject_path: Path, version: str) -> bool:
    text = pyproject_path.read_text()
    new_text, n = ACAPY_DEP_RE.subn(rf"\g<1>{version}\g<2>", text)
    if n == 0:
        print(
            f"warning: no acapy-agent dependency line found in {pyproject_path}",
            file=sys.stderr,
        )
        return False
    pyproject_path.write_text(new_text)
    return True


_ENV_SNAPSHOT_SENTINEL = "___TEST_ACAPY_VERSION_ENV_SNAPSHOT___"


def env_from_sourced_script(script: Path) -> dict[str, str]:
    """Run a shell script with `.` (source) and return the environment it leaves
    behind, so variables it exports (e.g. init-network.sh's SUBNET/SUBNET_PREFIX)
    are visible to the docker compose calls that follow it.
    """
    result = subprocess.run(
        ["sh", "-c", f"set -a; . ./{script.name}; echo {_ENV_SNAPSHOT_SENTINEL}; env"],
        cwd=script.parent,
        capture_output=True,
        text=True,
    )
    script_output, _, env_dump = result.stdout.partition(_ENV_SNAPSHOT_SENTINEL + "\n")
    print(script_output, end="")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return dict(os.environ)

    env = dict(os.environ)
    for line in env_dump.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    return env


def run(cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> int:
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


def compose_down(integration_dir: Path) -> None:
    subprocess.run(
        ["docker", "compose", "down", "--remove-orphans", "--rmi", "local"],
        cwd=integration_dir,
        stderr=subprocess.DEVNULL,
    )


def test_plugin(plugin: str, version: str, touched_files: list[str]) -> str:
    print()
    print(f"== [{plugin}] pinning acapy-agent == {version} ==")
    plugin_dir = REPO_ROOT / plugin
    pyproject = plugin_dir / "pyproject.toml"
    lock = plugin_dir / "poetry.lock"

    if not pyproject.exists():
        print(f"skip: {pyproject} not found")
        return "no-pyproject"

    if not set_version(pyproject, version):
        return "no-acapy-dep"
    touched_files.append(str(pyproject.relative_to(REPO_ROOT)))

    print(f"== [{plugin}] regenerating poetry.lock ==")
    if run(["poetry", "lock"], cwd=plugin_dir) != 0:
        print(f"FAIL: [{plugin}] poetry lock failed (version {version} likely unresolvable)")
        if lock.exists():
            touched_files.append(str(lock.relative_to(REPO_ROOT)))
        return "lock-failed"
    touched_files.append(str(lock.relative_to(REPO_ROOT)))

    if plugin in SKIP_TESTS:
        print(f"skip: [{plugin}] integration tests skipped (matches CI)")
        return "skipped"

    integration_dir = plugin_dir / "integration"
    if not (integration_dir / "docker-compose.yml").exists():
        print(f"skip: [{plugin}] no integration/docker-compose.yml")
        return "no-integration-tests"

    # init-network.sh (only used by cache_redis) exports a randomized SUBNET/
    # SUBNET_PREFIX and creates a standalone "acapy_default" docker network
    # with that subnet. That's separate from the "integration_acapy_default"
    # network docker compose creates for the stack itself. Passing the same
    # SUBNET into `compose up` would make the two collide ("pool overlaps"),
    # so its env is scoped to the build step only and `up`/`run` fall back to
    # docker-compose.yml's own default subnet, matching the working behavior
    # this replaces.
    init_network = integration_dir / "init-network.sh"
    build_env = env_from_sourced_script(init_network) if init_network.exists() else dict(os.environ)

    print(f"== [{plugin}] docker compose build ==")
    if run(["docker", "compose", "build"], cwd=integration_dir, env=build_env) != 0:
        print(f"FAIL: [{plugin}] docker compose build failed")
        compose_down(integration_dir)
        return "build-failed"

    print(f"== [{plugin}] running integration tests ==")
    if plugin == "cache_redis":
        test_exit = run(["docker", "compose", "up", "-d"], cwd=integration_dir)
        if test_exit == 0:
            test_exit = run(["docker", "compose", "run", "--rm", "tests"], cwd=integration_dir)
    else:
        test_exit = run(
            ["docker", "compose", "up", "--exit-code-from", "tests"],
            cwd=integration_dir,
        )

    compose_down(integration_dir)

    return "pass" if test_exit == 0 else "fail"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test plugins' integration suites against a specific acapy-agent version.",
    )
    parser.add_argument("version", help="acapy-agent version to test, e.g. 1.7.0rc0")
    parser.add_argument(
        "plugins", nargs="*", help="Plugins to test (default: all discovered plugins)"
    )
    args = parser.parse_args()

    os.chdir(REPO_ROOT)
    check_clean()

    all_plugins = discover_plugins()
    plugins = args.plugins or all_plugins

    touched_files: list[str] = []
    results: dict[str, str] = {}
    try:
        for plugin in plugins:
            results[plugin] = test_plugin(plugin, args.version, touched_files)
    finally:
        revert(touched_files)

    print()
    print(f"================ Summary (acapy-agent {args.version}) ================")
    for plugin in plugins:
        print(f"{plugin:<30} {results.get(plugin, 'not-run')}")


if __name__ == "__main__":
    main()
