#!/usr/bin/env python3

import yaml
import os
import sys
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

DEFAULT_MCP_PORTS = {"rag":"8050",
                     "code":"8051",
                     "web_search":"8052"}


def canonical_service_name(name: str) -> str:
    return name.strip().replace("_", "-")


def env_name(name: str) -> str:
    return name.replace("-", "_")


def expand_service(name, service, replicas):
    services = {}

    for i in range(1, replicas + 1):
        s = deepcopy(service)

        if "ports" in s:
            ports = s.pop("ports")
            s["expose"] = [p.split("}:")[1] if "}:" in p else p.split(":")[-1] for p in ports]

        services[f"{name}-{i}"] = s

    return services


def haproxy_backend(name, port, replicas, sticky_mode=None):
    lines = []
    lines.append(f"backend be_{name}")
    if sticky_mode:
        lines.append(f"    balance {sticky_mode}")

    for i in range(1, replicas + 1):
        lines.append(f"    server {name}{i} {name}-{i}:{port} check")

    lines.append("")
    return "\n".join(lines)


def generate_haproxy(backend_n, backend_port, litellm_n, server_list, replica_dict, port_dict):
    conf = []

    conf.append(
        "global\n"
        "    daemon\n"
        "    maxconn 256\n"
        "\n"
        "defaults\n"
        "    mode http\n"
        "    timeout connect 5s\n"
        "    timeout client  60s\n"
        "    timeout server  60s\n"
        "    default-server inter 3s fall 3 rise 2\n"
        "\n"
    )

    conf.append(
        "frontend fe_backend\n"
        f"    bind *:{backend_port}\n"
        "    default_backend be_freva-gpt-backend\n"
        "\n"
    )

    conf.append(
        "frontend fe_litellm\n"
        f"    bind *:4000\n"
        "    default_backend be_litellm\n"
        "\n"
    )

    for s in server_list:
        service_name = canonical_service_name(s)
        conf.append(
            f"frontend fe_{service_name}\n"
            f"    bind *:{port_dict[s]}\n"
            f"    default_backend be_{service_name}\n"
            "\n"
        )

    conf.append(
        haproxy_backend(
            "freva-gpt-backend",
            backend_port,
            backend_n,
            "url_param thread_id",
        )
    )

    conf.append(
        haproxy_backend(
            "litellm",
            4000,
            litellm_n,
        )
    )

    for s in server_list:
        conf.append(
            haproxy_backend(
                canonical_service_name(s),
                port_dict[s],
                replica_dict[s],
                "hdr(thread_id)",
            )
        )

    return "\n".join(conf)


def main():

    if len(sys.argv) < 2:
        print("Usage: gen_compose.py docker-compose.dev.yml")
        sys.exit(1)

    compose_path = sys.argv[1]

    backend_port = os.environ.get("FREVAGPT_BACKEND_PORT", "8502")
    backend_target_port = os.environ.get("FREVAGPT_TARGET_PORT", "8502")
    backend_n = int(os.environ.get("FREVAGPT_BACKEND_REPLICAS", "1"))
    litellm_n = int(os.environ.get("FREVAGPT_LITELLM_REPLICAS", "1"))

    available_mcp_servers = [
        canonical_service_name(s)
        for s in os.environ.get("FREVAGPT_AVAILABLE_MCP_SERVERS", "").split(",")
        if s.strip()
    ]
    mcp_replica_n = {
        s: int(os.environ.get(f"FREVAGPT_{env_name(s)}_REPLICAS", "1"))
        for s in available_mcp_servers
    }
    port_dict = {
        s: os.environ.get(f"FREVAGPT_{env_name(s).upper()}_PORT", DEFAULT_MCP_PORTS.get(env_name(s)))
        for s in available_mcp_servers
    }

    base = yaml.safe_load(open(compose_path))

    services = base["services"]
    new_services = {}

    for name, svc in services.items():
        if name == "freva-gpt-backend":
            new_services.update(expand_service(name, svc, backend_n))
        elif name == "litellm":
            new_services.update(expand_service(name, svc, litellm_n))
        elif name in available_mcp_servers:
            new_services.update(expand_service(name, svc, mcp_replica_n[name]))
        else:
            new_services[name] = svc

    dev_ports = [
            f"{backend_target_port}:{backend_port}",
            f"{port_dict.get('code')}:{port_dict.get('code')}"
        ]
    prod_ports = [
            f"{backend_target_port}:{backend_port}",
        ]
    
    network_name = list(base["networks"].keys())[0]

    new_services["haproxy"] = {
        "image": "haproxy:3.0-alpine",
        "user": "0:0",
        "ports": dev_ports if "dev" in compose_path else prod_ports,
        "volumes": [
            "./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro"
        ],
        "networks": [network_name],
        "depends_on": list(new_services.keys()),
    }

    out = {
        "services": new_services,
        "networks": base["networks"]
    }

    input_path = Path(compose_path)

    output_path = input_path.with_name(
        f"{input_path.stem}.scaled{input_path.suffix}"
    )

    output_path.write_text(
        yaml.dump(out, sort_keys=False)
    )

    haproxy_cfg = generate_haproxy(
        backend_n,
        backend_port,
        litellm_n,
        available_mcp_servers,
        mcp_replica_n,
        port_dict,
    )

    Path("haproxy.cfg").write_text(haproxy_cfg)

    print(f"Generated {output_path.name} and haproxy.cfg")


if __name__ == "__main__":
    main()