import asyncio
import os
import psutil
import json

from src.services.streaming.stream_variants import SVServerHint


async def heartbeat_content():
    """
    Collects system heartbeat info — CPU, memory, process stats —
    and returns it as a JSON string .
    """

    heartbeat = {}

    # Simulate "maybe_update" — refresh system metrics
    psutil.cpu_percent(interval=None)
    psutil.virtual_memory()

    # Current process info
    pid = os.getpid()
    proc = psutil.Process(pid)

    # --- Memory Info ---
    mem = psutil.virtual_memory()
    heartbeat["memory"] = mem.used
    heartbeat["total_memory"] = mem.total

    # --- CPU Info ---
    heartbeat["cpu_usage"] = psutil.cpu_percent(interval=None)
    heartbeat["cpu_last_minute"] = psutil.getloadavg()[0]  # 1-minute load average

    # --- Process Tree: include self and all descendants ---
    process_list = [pid]
    found_some = True
    while found_some:
        found_some = False
        for p in psutil.process_iter(['pid', 'ppid']):
            if p.info['ppid'] in process_list and p.info['pid'] not in process_list:
                process_list.append(p.info['pid'])
                found_some = True

    # --- Aggregate CPU and memory for those processes ---
    process_cpu = 0.0
    process_memory = 0
    for p in psutil.process_iter(['pid', 'cpu_percent', 'memory_info']):
        if p.info['pid'] in process_list:
            try:
                process_cpu += p.info['cpu_percent']
                process_memory += p.info['memory_info'].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    heartbeat["process_cpu"] = process_cpu
    heartbeat["process_memory"] = process_memory

    # Return as StreamVariant::ServerHint
    return SVServerHint(data=heartbeat)
