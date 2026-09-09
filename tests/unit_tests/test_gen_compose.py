import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

# gen_compose.py is a repo-root script, while pytest only adds src/ to sys.path.
GEN_COMPOSE_PATH = Path(__file__).resolve().parents[2] / "gen_compose.py"
spec = importlib.util.spec_from_file_location("gen_compose", GEN_COMPOSE_PATH)
assert spec is not None
assert spec.loader is not None
gen_compose = importlib.util.module_from_spec(spec)
sys.modules["gen_compose"] = gen_compose
spec.loader.exec_module(gen_compose)


def test_project_mapping_returns_preview_mounts_and_website():
    assert gen_compose.preview_paths_for_project("nextgems") == [
        "/work/ch1187/clint/freva-dev/share/preview/climateclaw",
        "/work/ch1187/clint/nextgems/share/preview/climateclaw",
    ]
    assert gen_compose.website_for_project("nextgems") == "https://gems.dkrz.de"


def test_unknown_project_exits_with_valid_project_names(capsys):
    with pytest.raises(SystemExit) as exc_info:
        gen_compose.preview_paths_for_project("unknown")

    assert exc_info.value.code == 1
    assert "ERROR: unknown project 'unknown'" in capsys.readouterr().err


def test_set_environment_adds_value_when_environment_missing():
    service = {}

    gen_compose.set_environment(service, "KEY", "value")

    assert service["environment"] == ["KEY=value"]


def test_set_environment_updates_dict_environment():
    service = {"environment": {"KEY": "old", "OTHER": "kept"}}

    gen_compose.set_environment(service, "KEY", "new")

    assert service["environment"] == {"KEY": "new", "OTHER": "kept"}


def test_set_environment_replaces_list_environment_value():
    service = {"environment": ["KEY=old", "OTHER=kept"]}

    gen_compose.set_environment(service, "KEY", "new")

    assert service["environment"] == ["OTHER=kept", "KEY=new"]


def test_set_environment_rejects_unknown_environment_shape():
    with pytest.raises(
        TypeError, match="service environment must be a mapping or a list"
    ):
        gen_compose.set_environment({"environment": "KEY=old"}, "KEY", "new")


def test_expand_service_adds_preview_mounts_to_replicas():
    services = gen_compose.expand_service(
        "code-server",
        {"volumes": ["/work:/work:ro"]},
        replicas=2,
        preview_paths=["/preview/a", "/preview/b"],
    )

    assert services["code-server-1"]["volumes"] == [
        "/work:/work:ro",
        "/preview/a:/app/cache:rw",
        "/preview/b:/app/cache:rw",
    ]
    assert services["code-server-2"]["volumes"] == [
        "/work:/work:ro",
        "/preview/a:/app/cache:rw",
        "/preview/b:/app/cache:rw",
    ]


def test_expand_service_without_preview_paths_keeps_volumes_unchanged():
    services = gen_compose.expand_service(
        "web-search-server",
        {"volumes": ["/logs:/app/logs"]},
        replicas=1,
    )

    assert services["web-search-server-1"]["volumes"] == ["/logs:/app/logs"]


def test_generated_compose_sets_project_env_and_code_server_mounts(
    tmp_path,
    monkeypatch,
):
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(
        yaml.dump(
            {
                "services": {
                    "climateclaw": {
                        "image": "climateclaw",
                        "environment": ["EXISTING=kept"],
                        "networks": ["climateclaw"],
                    },
                    "code-server": {
                        "image": "code-server",
                        "volumes": ["/work:/work:ro"],
                        "networks": ["climateclaw"],
                    },
                    "mongodb": {
                        "image": "mongodb",
                        "networks": ["climateclaw"],
                    },
                },
                "networks": {"climateclaw": {"driver": "bridge"}},
            },
            sort_keys=False,
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["gen_compose.py", str(compose_path), "codes"],
    )
    monkeypatch.setenv("CLIMATECLAW_BACKEND_REPLICAS", "1")
    monkeypatch.setenv("CLIMATECLAW_LITELLM_REPLICAS", "1")
    monkeypatch.setenv("CLIMATECLAW_OLLAMA_REPLICAS", "1")
    monkeypatch.setenv("CLIMATECLAW_AVAILABLE_MCP_SERVERS", "")

    gen_compose.main()

    generated = yaml.safe_load((tmp_path / "docker-compose.scaled.yml").read_text())
    climateclaw_env = generated["services"]["climateclaw-1"]["environment"]
    assert climateclaw_env == [
        "EXISTING=kept",
        "CLIMATECLAW_PROJECT_NAME=codes",
        "CLIMATECLAW_PROJECT_WEBSITE=https://codes.dkrz.de",
    ]
    assert generated["services"]["code-server-1"]["volumes"] == [
        "/work:/work:ro",
        "/work/kd1418/codes/work/share/preview/climateclaw:/app/cache:rw",
    ]
