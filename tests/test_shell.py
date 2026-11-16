"""Shell slice tests: compose file, env template and README (AC26-AC30).

One test per acceptance criterion in ``spec/requirements.md``.  The compose
test runs the real ``docker compose config``, offline, with no ``.env`` file
present; nothing here starts a container.
"""

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from music import _contract as contract

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"
README = ROOT / "README.md"
DEVELOPMENT = ROOT / "docs" / "DEVELOPMENT.md"
ENV_FILE = ROOT / ".env"

# Every variable the compose file and the pipeline may read.
COMPOSE_VARS = (
    "NAVIDROME_BIND_ADDRESS",
    "NAVIDROME_PORT",
    "NAVIDROME_ADMIN_USER",
    "NAVIDROME_ADMIN_PASSWORD",
)
PIPELINE_VARS = tuple(contract.ENV_VARS.values())
# The optional overrides (AC39, AC42).
TOOL_VARS = (
    "MUSIC_FFMPEG",
    "MUSIC_FFPROBE",
    "MUSIC_CONCURRENCY",
    "MUSIC_PLAYLISTS_EXPORT",
)


def _docs() -> str:
    """The documentation a reader gets: the README and the file it links."""
    return _read(README) + "\n" + _read(DEVELOPMENT)

# The only value that makes `docker compose config` succeed (AC31).
PASSWORD_ENV = {"NAVIDROME_ADMIN_PASSWORD": "placeholder-from-env"}

IMAGE_RE = re.compile(r"^deluan/navidrome:[^@]+@sha256:[0-9a-f]{64}$")

# A credential-looking assignment: password/secret/token/key followed by = or :.
CREDENTIAL_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|access[_-]?key)"
    r"\s*[:=]\s*(.+)$"
)
# Values that are plainly placeholders, not credentials.
PLACEHOLDER_RE = re.compile(
    r"^(?:$|change-?me|placeholder|TODO|<[^>]*>|\$\{[^}]*\}"
    r"|your[-_ ].*|example[-_ ].*)$",
    re.IGNORECASE,
)


def _clean_env(extra=None):
    """Environment without any variable that could fill in the compose file."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MUSIC_", "NAVIDROME_"))
    }
    if extra:
        env.update(extra)
    return env


def _compose_config(*args, extra_env=None):
    """Run `docker compose config` for real and return the completed process."""
    return subprocess.run(
        ["docker", "compose", "config", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_clean_env(extra_env),
    )


def _read(path):
    return path.read_text(encoding="utf-8")


def _env_example_keys():
    keys = {}
    for line in _read(ENV_EXAMPLE).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        keys[key.strip()] = value.strip()
    return keys


class ShellTestCase(unittest.TestCase):
    def setUp(self):
        # AC26 runs without a .env file: remove it, then restore it afterwards
        # so a developer's local configuration survives the test run.
        self._env_backup = ENV_FILE.read_bytes() if ENV_FILE.exists() else None
        if ENV_FILE.exists():
            ENV_FILE.unlink()

    def tearDown(self):
        if self._env_backup is not None:
            ENV_FILE.write_bytes(self._env_backup)

    # AC26 ---------------------------------------------------------------
    def test_ac26_compose_config_validates_without_env(self):
        self.assertFalse(ENV_FILE.exists(), ".env must be absent for this test")
        # AC31 makes NAVIDROME_ADMIN_PASSWORD mandatory, so validation without
        # a .env file is checked with the password supplied from the
        # environment. Nothing here pulls an image.
        proc = _compose_config("--quiet", extra_env=PASSWORD_ENV)
        self.assertEqual(
            proc.returncode,
            0,
            f"docker compose config failed without .env:\n{proc.stderr}",
        )
        self.assertNotIn("Pulling", proc.stdout + proc.stderr)

    # AC27 ---------------------------------------------------------------
    def test_ac27_env_ignored_example_tracked_no_credential(self):
        ignore_entries = {
            line.strip()
            for line in _read(GITIGNORE).splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertIn(".env", ignore_entries, ".gitignore must list .env")

        def tracked(path):
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", path],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            return result.returncode == 0

        self.assertTrue(ENV_EXAMPLE.is_file(), ".env.example must exist")
        if not tracked(".env.example"):
            # This worktree is not committed yet, so a new file cannot be in
            # the index. It still must be included in the repository: present
            # and not ignored.
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", ".env.example"],
                cwd=ROOT,
                capture_output=True,
            )
            self.assertNotEqual(
                ignored.returncode, 0, ".env.example must not be ignored"
            )
        self.assertFalse(tracked(".env"), ".env must not be tracked")
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True
        )
        self.assertEqual(ignored.returncode, 0, ".env must be ignored")

        files = subprocess.run(
            ["git", "ls-files"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        for rel in files:
            if rel.startswith("tests/"):
                # Test modules quote environment-variable names and compose
                # interpolations; they never ship a credential value.
                continue
            path = ROOT / rel
            if not path.is_file():
                continue
            try:
                text = _read(path)
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                match = CREDENTIAL_RE.search(line)
                if not match:
                    continue
                # A compose interpolation is a placeholder even when it is
                # followed by prose: keep only the value up to a quote/comma.
                raw = match.group(1).strip()
                value = re.split(r"[\"',]", raw, maxsplit=1)[0].strip()
                self.assertRegex(
                    value,
                    PLACEHOLDER_RE,
                    f"credential value in {rel}:{number}: {line.strip()}",
                )

    # AC28 ---------------------------------------------------------------
    def test_ac28_readme_names_every_variable(self):
        readme = _docs()
        compose_text = _read(COMPOSE_FILE)
        compose_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_text))
        self.assertEqual(
            compose_vars,
            set(COMPOSE_VARS) | {"MUSIC_LIBRARY_ROOT"},
            "compose must interpolate exactly the documented variables",
        )
        example_keys = set(_env_example_keys())
        for name in PIPELINE_VARS + TOOL_VARS + COMPOSE_VARS:
            self.assertIn(name, readme, f"README must name {name}")
            self.assertIn(name, example_keys, f".env.example must name {name}")
        self.assertIn("required", readme.lower())
        self.assertIn("untracked", readme.lower())

    # AC29 ---------------------------------------------------------------
    def test_ac29_readme_has_connect_a_client_section(self):
        readme = _docs()
        headings = list(
            re.finditer(r"(?m)^(#{1,6})\s+Connect a client\s*$", readme)
        )
        self.assertEqual(
            len(headings), 1, 'README needs one "Connect a client" section'
        )
        level = len(headings[0].group(1))
        section = readme[headings[0].end():]
        next_heading = re.search(
            r"(?m)^#{1,%d}\s+\S" % level, section
        )
        if next_heading:
            section = section[: next_heading.start()]
        self.assertIn("https://", section)
        self.assertNotIn("http://", section)
        self.assertIn("Amperfy", section)
        self.assertIn("iOS", section)
        self.assertIn("SonicLair", section)
        self.assertIn("Android TV", section)

    # AC30 ---------------------------------------------------------------
    def test_ac30_compose_mounts_library_root_and_reads_env(self):
        compose_text = _read(COMPOSE_FILE)
        self.assertIn("${MUSIC_LIBRARY_ROOT:-", compose_text)
        self.assertIn("${NAVIDROME_PORT:-4533}", compose_text)
        self.assertIn("${NAVIDROME_ADMIN_PASSWORD:?", compose_text)
        example_keys = _env_example_keys()
        self.assertIn("MUSIC_LIBRARY_ROOT", example_keys)
        self.assertIn("NAVIDROME_PORT", example_keys)
        self.assertIn("NAVIDROME_ADMIN_PASSWORD", example_keys)

        proc = _compose_config("--format", "json", extra_env=PASSWORD_ENV)
        self.assertEqual(
            proc.returncode, 0, f"docker compose config failed:\n{proc.stderr}"
        )
        config = json.loads(proc.stdout)
        service = config["services"]["navidrome"]
        self.assertRegex(
            service["image"],
            IMAGE_RE,
            "the image must be tagged and pinned by digest (AC33)",
        )

        volumes = service["volumes"]
        self.assertEqual(len(volumes), 1, "exactly one volume: the library root")
        volume = volumes[0]
        self.assertEqual(volume["target"], "/music")
        self.assertEqual(volume["type"], "bind")
        self.assertEqual(Path(volume["source"]), (ROOT / "library").resolve())
        self.assertTrue(volume["read_only"], "/music is read-only for Navidrome")

        self.assertEqual(service["ports"][0]["published"], "4533")
        self.assertEqual(service["ports"][0]["host_ip"], "127.0.0.1")

        # The port and the admin password come from the environment, not from
        # a hard-coded value in the compose file.
        proc = _compose_config(
            "--format",
            "json",
            extra_env={
                "NAVIDROME_PORT": "8123",
                "NAVIDROME_ADMIN_PASSWORD": "placeholder-from-env",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        service = json.loads(proc.stdout)["services"]["navidrome"]
        self.assertEqual(service["ports"][0]["published"], "8123")
        environment = service["environment"]
        self.assertEqual(
            environment["ND_DEVAUTOCREATEADMINPASSWORD"],
            "placeholder-from-env",
        )
        self.assertNotIn("placeholder-from-env", _read(COMPOSE_FILE))

    # AC31 ---------------------------------------------------------------
    def test_ac31_compose_fails_without_admin_password(self):
        self.assertFalse(ENV_FILE.exists(), ".env must be absent for this test")
        proc = _compose_config()
        self.assertNotEqual(
            proc.returncode,
            0,
            "compose must refuse to run without NAVIDROME_ADMIN_PASSWORD",
        )
        self.assertIn("NAVIDROME_ADMIN_PASSWORD", proc.stdout + proc.stderr)
        self.assertNotIn("change-me", _read(COMPOSE_FILE))
        self.assertNotIn("change-me", _read(ENV_EXAMPLE))

        proc = _compose_config(
            "--format",
            "json",
            extra_env={"NAVIDROME_ADMIN_PASSWORD": "a-real-operator-password"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        environment = json.loads(proc.stdout)["services"]["navidrome"][
            "environment"
        ]
        self.assertEqual(
            environment["ND_DEVAUTOCREATEADMINPASSWORD"],
            "a-real-operator-password",
        )

    # AC32 ---------------------------------------------------------------
    def test_ac32_published_port_binds_loopback_by_default(self):
        proc = _compose_config("--format", "json", extra_env=PASSWORD_ENV)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        port = json.loads(proc.stdout)["services"]["navidrome"]["ports"][0]
        self.assertEqual(port["host_ip"], "127.0.0.1")
        self.assertEqual(port["target"], 4533)

        proc = _compose_config(
            "--format",
            "json",
            extra_env={**PASSWORD_ENV, "NAVIDROME_BIND_ADDRESS": "0.0.0.0"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        port = json.loads(proc.stdout)["services"]["navidrome"]["ports"][0]
        self.assertEqual(
            port["host_ip"], "0.0.0.0", "an operator variable widens the bind"
        )

    # AC33 ---------------------------------------------------------------
    def test_ac33_image_pinned_by_digest_and_readme_uses_https(self):
        compose_text = _read(COMPOSE_FILE)
        image_lines = [
            line.strip()
            for line in compose_text.splitlines()
            if line.strip().startswith("image:")
        ]
        self.assertEqual(len(image_lines), 1, "one image line in the compose file")
        self.assertIn("@sha256:", image_lines[0])
        self.assertRegex(image_lines[0].split("image:", 1)[1].strip(), IMAGE_RE)

        readme = _docs()
        match = re.search(r"(?m)^(#{1,6})\s+Connect a client\s*$", readme)
        self.assertIsNotNone(match, 'README needs a "Connect a client" section')
        section = readme[match.end():]
        next_heading = re.search(r"(?m)^#{1,%d}\s+\S" % len(match.group(1)), section)
        if next_heading:
            section = section[: next_heading.start()]
        self.assertIn("https://", section, "clients are given an https URL")


if __name__ == "__main__":
    unittest.main()
