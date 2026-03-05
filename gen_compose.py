#!/usr/bin/env python3

import yaml
import os
import sys
from copy import deepcopy
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

#TODO debug port for dev

def expand_service(name, service, replicas):
    services = {}

    for i in range(1, replicas + 1):
        s = deepcopy(service)

        if "ports" in s:
            ports = s.pop("ports")
            s["expose"] = [p.split(":")[-1].strip("-").strip("}") for p in ports]

        services[f"{name}-{i}"] = s

    return services


def nginx_upstream(name, port, replicas, sticky):
    lines = []
    lines.append(f"  upstream {name}_pool {{")
    lines.append(f"    hash {sticky} consistent;")

    for i in range(1, replicas + 1):
        lines.append(f"    server {name}-{i}:{port};")

    lines.append("  }\n")
    return "\n".join(lines)


def generate_nginx(backend_n, server_list, replica_dict):

    backend_port = os.environ.get("FREVAGPT_BACKEND_PORT", "8502")
    port_dict = {s: os.environ.get(f"FREVAGPT_{s.upper()}_PORT", "") for s in server_list}

    conf = []

    conf.append("worker_processes auto;\n")
    conf.append("events {\n"\
                "   worker_connections 1024;\n"\
                "}\n")
    conf.append("http {\n")

    conf.append(nginx_upstream(
        "freva-gpt-backend",
        backend_port,
        backend_n,
        "$arg_thread_id"
    ))

    for s in server_list:
        conf.append(nginx_upstream(
            s,
            port_dict[s],
            replica_dict[s],
            "$http_thread_id"
        ))


    conf.append(
        "  server {\n"\
        f"    listen {backend_port};\n"\
        "    location / {\n"\
        "      proxy_pass http://freva-gpt-backend_pool;\n"\
        "    }\n"\
        "  }\n"
    )
    
    for s in server_list:
        conf.append(
            "  server {\n"\
            f"    listen {port_dict[s]};\n"\
            "    location / {\n"\
            f"      proxy_pass http://{s}_pool;\n"\
            "    }\n"\
            "  }\n"
        )

    conf.append("}")
    return "\n".join(conf)


def main():

    if len(sys.argv) < 2:
        print("Usage: gen_compose.py docker-compose.dev.yml")
        sys.exit(1)

    compose_path = sys.argv[1]

    backend_port = os.environ.get("FREVAGPT_BACKEND_PORT", "8502")
    backend_n = int(os.environ.get("FREVAGPT_BACKEND_REPLICAS", "1"))

    available_mcp_servers = [s for s in os.environ.get("FREVAGPT_AVAILABLE_MCP_SERVERS", []).split(",")]
    mcp_replica_n = {s: int(os.environ.get(f"FREVAGPT_{s.upper()}_REPLICAS", "1")) for s in available_mcp_servers}

    base = yaml.safe_load(open(compose_path))

    services = base["services"]
    new_services = {}

    for name, svc in services.items():

        if name == "freva-gpt-backend":
            new_services.update(expand_service(name, svc, backend_n))

        elif name in available_mcp_servers:
            new_services.update(expand_service(name, svc, mcp_replica_n[name]))

        else:
            new_services[name] = svc

    new_services["nginx"] = {
        "image": "nginx:alpine",
        "env_file": ".env",
        "ports": [
            f"{backend_port}:{backend_port}",
        ],
        "volumes": [
            "./nginx.conf:/etc/nginx/nginx.conf:ro"
        ],
        "networks": ["freva-gpt"],
        "depends_on": list(new_services.keys())
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

    nginx = generate_nginx(backend_n, available_mcp_servers, mcp_replica_n)

    Path("nginx.conf").write_text(nginx)

    print("Generated docker-compose.scaled.yml and nginx.conf")


if __name__ == "__main__":
    main()