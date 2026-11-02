"""The CLI resolves its settings from flags, the environment and secret files.

A secret on the command line is readable by every process on the host
(``ps``, ``/proc/<pid>/cmdline``), so a container must be able to start the
bridge with no secret in its arguments at all.
"""
import pytest

from hivemind_telegram_bridge.__main__ import resolve_options


def test_everything_can_come_from_the_environment():
    options = resolve_options([], {
        "TELEGRAM_BOT_TOKEN": "tok",
        "TELEGRAM_ALLOWED_CHATS": "42, -100123",
        "HIVEMIND_ACCESS_KEY": "key",
        "HIVEMIND_PASSWORD": "pw",
        "HIVEMIND_HOST": "hub.local",
        "HIVEMIND_PORT": "6000",
        "HIVEMIND_SITE_ID": "kitchen",
        "HIVEMIND_SELF_SIGNED": "true",
        "HIVEMIND_LANG": "fr-fr",
    })
    assert options == {
        "token": "tok",
        "key": "key",
        "password": "pw",
        "host": "ws://hub.local",
        "port": 6000,
        "self_signed": True,
        "lang": "fr-fr",
        "site_id": "kitchen",
        "allowed_chats": [42, -100123],
        "require_mention": True,
    }


def test_secrets_can_come_from_files(tmp_path):
    token = tmp_path / "token"
    token.write_text("tok-from-file\n", encoding="utf-8")
    password = tmp_path / "password"
    password.write_text("pw-from-file", encoding="utf-8")
    options = resolve_options([], {
        "TELEGRAM_BOT_TOKEN_FILE": str(token),
        "HIVEMIND_PASSWORD_FILE": str(password),
    })
    assert options["token"] == "tok-from-file"
    assert options["password"] == "pw-from-file"


def test_a_flag_wins_over_the_environment_and_the_environment_over_a_file(tmp_path):
    token = tmp_path / "token"
    token.write_text("from-file", encoding="utf-8")
    environ = {"TELEGRAM_BOT_TOKEN": "from-env", "TELEGRAM_BOT_TOKEN_FILE": str(token)}
    assert resolve_options(["--token", "from-flag"], environ)["token"] == "from-flag"
    assert resolve_options([], environ)["token"] == "from-env"


def test_defaults_when_nothing_but_the_token_is_given():
    options = resolve_options(["--token", "tok"], {})
    assert options["port"] == 5678
    assert options["site_id"] == "telegram"
    assert options["lang"] == "en-us"
    assert options["self_signed"] is False
    assert options["allowed_chats"] is None
    assert options["host"] is None


def test_a_missing_token_is_a_usage_error(capsys):
    with pytest.raises(SystemExit):
        resolve_options([], {})
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_an_unreadable_secret_file_names_the_variable(tmp_path):
    with pytest.raises(SystemExit) as stopped:
        resolve_options([], {"TELEGRAM_BOT_TOKEN_FILE": str(tmp_path / "absent")})
    assert "TELEGRAM_BOT_TOKEN_FILE" in str(stopped.value)


def test_a_malformed_chat_list_is_a_usage_error():
    with pytest.raises(SystemExit):
        resolve_options(["--token", "tok"], {"TELEGRAM_ALLOWED_CHATS": "42,general"})


def test_the_image_passes_no_secret_on_the_command_line():
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = next(line for line in dockerfile.splitlines() if line.startswith("ENTRYPOINT"))
    for secret in ("--token", "--password", "--access-key", "$TELEGRAM_BOT_TOKEN",
                   "$HIVEMIND_PASSWORD"):
        assert secret not in entrypoint


def test_mention_gating_is_on_by_default_and_can_be_turned_off():
    assert resolve_options(["--token", "tok"], {})["require_mention"] is True
    assert resolve_options(["--token", "tok", "--no-require-mention"], {})["require_mention"] is False
    assert resolve_options(["--token", "tok"], {"TELEGRAM_REQUIRE_MENTION": "false"})["require_mention"] is False
