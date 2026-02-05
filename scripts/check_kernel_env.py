from __future__ import annotations

import json
import os
import sys

from jupyter_client import KernelManager
from jupyter_client.blocking import BlockingKernelClient

freva_config_path = "/work/ch1187/clint/nextgems/freva/evaluation_system.conf"
freva_env_var = {"EVALUATION_SYSTEM_CONFIG_FILE": freva_config_path}
os.environ["EVALUATION_SYSTEM_CONFIG_FILE"] = freva_config_path

_KERNEL_REGISTRY: dict[str, KernelManager] = {}

# ── Execution helpers ───────────────────────────────────────────────────────


def _get_or_start_kernel(
    sid: str, session_env: dict[str, str] | None = None
) -> KernelManager:
    km = _KERNEL_REGISTRY.get(sid)
    if km is None:
        # We preserve the env variables set in Dockerfile and add
        # freva-config-path
        env = os.environ.copy()
        if session_env:
            env.update({k: str(v) for k, v in session_env.items()})
        km = KernelManager()
        km.kernel_cmd = [
            sys.executable,
            "-m",
            "ipykernel",
            "-f",
            "{connection_file}",
        ]  # Otherwise "No such kernel named python3"
        km.start_kernel(env=env)
        _KERNEL_REGISTRY[sid] = km
    return km


def show_kernel_env(km: KernelManager, keys: list[str] | None = None):
    """Ask the kernel to print its environment (optionally filtered)."""
    kc: BlockingKernelClient = km.client()
    kc.start_channels()
    try:
        if keys:
            expr = (
                "{"
                + ", ".join(f"'{k}': os.environ.get('{k}')" for k in keys)
                + "}"
            )
        else:
            expr = "dict(list(os.environ.items())[:20])"  # limit output
        code = f"import os, json; print(json.dumps({expr}))"
        msg_id = kc.execute(
            code, store_history=False, allow_stdin=False, stop_on_error=True
        )
        print(msg_id)
        kc.get_shell_msg(timeout=5)
        while True:
            msg = kc.get_iopub_msg(timeout=5)
            if msg["header"]["msg_type"] == "stream":
                text = msg["content"]["text"].strip()
                try:
                    data = json.loads(text)
                    print(json.dumps(data, indent=2))
                    return data
                except json.JSONDecodeError:
                    print(text)
            if (
                msg["header"]["msg_type"] == "status"
                and msg["content"]["execution_state"] == "idle"
            ):
                break
    finally:
        kc.stop_channels()


def _run_cell(sid: str, code: str) -> dict:
    km = _get_or_start_kernel(sid, session_env=freva_env_var)
    kc = km.client()
    kc.start_channels()
    try:
        msg_id = kc.execute(
            code, store_history=True, allow_stdin=False, stop_on_error=False
        )
        stdout_parts, stderr_parts, display_data, result_repr, error = (
            [],
            [],
            [],
            None,
            None,
        )
        # There could be display_data that is sent with an id and these
        # can be updated later using msg_type="update_display_data".
        # For these, we keep only the last updated version.
        display_data_dict = {}

        # Since Jupyter kernel runs asynchronously, it streams outputs, errors,
        # and state messages while it executes the code.
        # We loop to collect them in real time until the status is "idle".
        while True:
            msg = kc.get_iopub_msg()
            # We check if the msg is from the cell we just executed, just in
            # case there are idle cells still emitting.
            # old/stale/background messages vs current cell
            if msg["parent_header"].get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]
            if (
                msg_type == "status"
                and msg["content"]["execution_state"] == "idle"
            ):
                break
            elif msg_type == "stream":
                (
                    stdout_parts
                    if msg["content"]["name"] == "stdout"
                    else stderr_parts
                ).append(msg["content"]["text"])
            elif msg_type in (
                "display_data",
                "update_display_data",
            ):  # Jupyter also returns rich outputs (image/png, text/html, etc.)
                display_id = (
                    msg["content"].get("transient", {}).get("display_id", "")
                )
                if display_id:
                    display_data_dict.update(
                        {display_id: msg["content"].get("data", {})}
                    )
                else:
                    display_data.append(msg["content"].get("data", {}))
            elif msg_type == "execute_result":
                result_repr = msg["content"].get("data", {}).get("text/plain")
            elif (
                msg_type == "error"
            ):  # Present only if an exception occurred. We record non-exception in stderr
                tb = "\n".join(msg["content"].get("traceback", []))
                error = (
                    tb
                    or f"{msg['content'].get('ename')}: {msg['content'].get('evalue')}"
                )

        # If we got any updated display in dict, we append them to the list.
        # Here, we are sending a list of unique output
        if display_data_dict:
            display_data.append(list(display_data_dict.values()))

        return {
            "stdout": "".join(stdout_parts),
            "stderr": "".join(stderr_parts),
            "result_repr": result_repr if result_repr else "",
            "display_data": display_data,
            "error": error if error else "",
        }
    finally:
        kc.stop_channels()


if __name__ == "__main__":
    sid = "env_test"

    code = "import os\n\nprint(os.getenv('EVALUATION_SYSTEM_CONFIG_FILE'))\n\nimport freva_client\nimport xarray as xr\n\n# Step 1: Load surface pressure data (ps) for January 1st, 2045 from a reliable dataset (e.g., ERA5)\n# Since data from 2045 is not available in current reanalysis datasets, I'll demonstrate with the procedure to load hypothetical data.\n\n# Let's assume we've run a future scenario model that has this data available.\ntime_str = '2045-01-01'\n\n# For demonstration purposes, I'll use existing data and assume it extends for 2045. Typically, you would access future projection datasets if available.\nfiles = freva_client.databrowser(project='reanalysis', experiment='era5', variable='ps', time_frequency='1hr', time=\"2045-01-01to2045-01-01\", time_select=\"flexible\")\n\n# Step 2: Load the hourly surface pressure data for January 1st, 2045\ndset = xr.open_mfdataset(list(files), combine='by_coords')\n\n# Step 3: Select the first available time slice of January 1st, 2045\nps_2045 = dset['ps'].isel(time=0)\n\nlist(files)"
    print(_run_cell(sid, code))
