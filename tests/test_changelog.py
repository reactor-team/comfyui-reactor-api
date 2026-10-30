import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("registry_changelog", ROOT / "scripts" / "registry_changelog.py")
registry_changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry_changelog)


def test_current_version_has_a_changelog_section():
    version = registry_changelog.project_version((ROOT / "pyproject.toml").read_text())
    section = registry_changelog.changelog_section((ROOT / "CHANGELOG.md").read_text(), version)
    assert section, f"CHANGELOG.md needs a `## {version}` section"


def test_section_stops_at_the_next_version():
    text = "# Changelog\n\n## 0.2.0\n\n- b\n\n## 0.1.0\n\n- a\n"
    assert registry_changelog.changelog_section(text, "0.2.0") == "- b"
    assert registry_changelog.changelog_section(text, "0.1.0") == "- a"
    assert registry_changelog.changelog_section(text, "0.3.0") is None
