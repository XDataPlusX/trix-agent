"""The Trix skin must load through the upstream skin engine unchanged."""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIN_PATH = REPO_ROOT / "assets/skins/trix.yaml"
CONFIG_EXAMPLE_PATH = REPO_ROOT / "cli-config.yaml.example"


def test_skin_file_is_valid_yaml_with_trix_branding():
    data = yaml.safe_load(SKIN_PATH.read_text(encoding="utf-8"))
    assert data["name"] == "trix"
    assert data["branding"]["agent_name"] == "Trix Agent"
    assert "Hermes" not in yaml.safe_dump(data, allow_unicode=True)


def test_skin_loads_through_engine(tmp_path, monkeypatch):
    import hermes_cli.skin_engine as skin_engine

    skins_dir = tmp_path / "skins"
    skins_dir.mkdir()
    (skins_dir / "trix.yaml").write_text(
        SKIN_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setattr(skin_engine, "get_hermes_home", lambda: tmp_path)

    skin = skin_engine.load_skin("trix")

    assert skin.branding["agent_name"] == "Trix Agent"


def test_skin_resolves_on_a_fresh_install_with_no_skins_directory(tmp_path, monkeypatch):
    """`skin: trix` in the shipped config must resolve WITHOUT any file ever
    being placed in $HERMES_HOME/skins/.

    Nothing in the install (install.sh, install.ps1, or the Docker image)
    copies assets/skins/trix.yaml into the customer's skins directory, so a
    real fresh install has no ~/.hermes/skins/trix.yaml at all -- unlike the
    test above, which manually seeds that file and therefore can't catch a
    registry-only regression. `trix` must be a registered built-in skin so
    load_skin() resolves it even when $HERMES_HOME/skins/ doesn't exist.
    """
    import hermes_cli.skin_engine as skin_engine

    # tmp_path exists but tmp_path/skins does not -- mirrors a fresh
    # $HERMES_HOME right after install, before any skin file was ever placed.
    monkeypatch.setattr(skin_engine, "get_hermes_home", lambda: tmp_path)
    assert not (tmp_path / "skins").exists()

    skin = skin_engine.load_skin("trix")

    assert skin.name == "trix"
    assert skin.branding["agent_name"] == "Trix Agent"
    assert "Hermes" not in skin.branding["welcome"]
    assert "/help" in skin.branding["welcome"]


def test_builtin_trix_skin_has_full_color_set_matching_slate():
    """assets/skins/trix.yaml must supply the same 28 color keys as the
    built-in `slate` skin it's derived from -- a partial palette silently
    falls through to the upstream GOLD default for the missing keys (status
    bar, session, completion menu, selection, shell prompt, voice status),
    producing a blue banner with a gold status bar.
    """
    import hermes_cli.skin_engine as skin_engine

    slate_colors = skin_engine._BUILTIN_SKINS["slate"]["colors"]
    trix_colors = yaml.safe_load(SKIN_PATH.read_text(encoding="utf-8"))["colors"]

    assert set(trix_colors.keys()) == set(slate_colors.keys())


def test_builtin_registry_and_skin_file_agree():
    """The built-in `trix` entry in skin_engine._BUILTIN_SKINS and
    assets/skins/trix.yaml are two independent copies of the same skin (the
    builtin is what actually loads on a fresh install; the file is kept for
    documentation / the `hermes skin` file-based workflow). They must stay
    in agreement or the two install paths would visibly disagree.
    """
    import hermes_cli.skin_engine as skin_engine

    builtin = skin_engine._BUILTIN_SKINS["trix"]
    from_file = yaml.safe_load(SKIN_PATH.read_text(encoding="utf-8"))

    assert builtin["colors"] == from_file["colors"]
    assert builtin["branding"]["agent_name"] == from_file["branding"]["agent_name"]
    assert builtin["branding"]["welcome"] == from_file["branding"]["welcome"]


def test_config_example_enables_trix_skin_by_default():
    """cli-config.yaml.example must resolve display.skin to trix.

    display: is a single YAML mapping and yaml.safe_load takes the LAST
    occurrence of a duplicate key. A skin: line added anywhere other than
    the one effective location silently loses to whatever the pre-existing
    "Skin / Theme" block sets, and copying the example verbatim would still
    seed the stock `default` theme.
    """
    data = yaml.safe_load(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))

    assert data["display"]["skin"] == "trix"


def test_code_defaults_agree_with_the_shipped_config_example():
    """The example only takes effect because install.sh copies it into
    ~/.hermes/config.yaml on a fresh install. Any OTHER path to a config --
    no config file at all, a config that predates the "Skin / Theme" block,
    hermes_cli/config.py's DEFAULT_CONFIG used by `hermes tools`/`hermes
    setup`/most subcommands, cli.py's separate CLI-only defaults dict used
    by load_cli_config() -- must resolve the same skin, or that path silently
    falls back to the upstream `default` theme. This is the fourth time this
    exact write/match drift has bitten this branch; assert the invariant so
    it can't happen a fifth time.
    """
    example = yaml.safe_load(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    expected_skin = example["display"]["skin"]

    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["display"]["skin"] == expected_skin


def test_cli_only_loader_resolves_the_same_skin_with_no_config_file(tmp_path, monkeypatch):
    """load_cli_config() (cli.py) carries its OWN hardcoded defaults dict,
    independent of hermes_cli.config_defaults.DEFAULT_CONFIG. With no
    config.yaml present at all (a fresh install before install.sh's copy
    step, or HERMES_IGNORE_USER_CONFIG), it must still resolve display.skin
    to the same value as the shipped example -- not silently fall back to
    the stock `default` theme.
    """
    import cli

    example = yaml.safe_load(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    expected_skin = example["display"]["skin"]

    monkeypatch.setattr(cli, "_hermes_home", tmp_path)
    assert not (tmp_path / "config.yaml").exists()

    resolved = cli.load_cli_config()

    assert resolved["display"]["skin"] == expected_skin
