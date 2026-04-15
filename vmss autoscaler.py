#!/usr/bin/env python3
# vmss_autoscaler.py - Reactive auto-scaler for nginx
# Checks CPU via prometheus, adds/removes servers from nginx upstream
# Awais - MSc Dissertation, Roehampton 2026

import subprocess
import requests
import time
import json
import os
import logging
from datetime import datetime

# Config
PROMETHEUS = "http://localhost:9090"
NGINX_CONF = "/etc/nginx/sites-enabled/default"
LOG_FILE = "/var/log/nginx/vmss_autoscaler.log"
EVENTS_LOG = "/var/log/nginx/vmss_scaling_events.json"

SCALE_UP = 75.0    # add server above this
SCALE_DOWN = 30.0  # remove server below this
CHECK_EVERY = 10   # seconds between checks
MIN_SERVERS = 1

# Backend server details
servers = [
    {"addr": "192.168.56.11", "name": "Backend-1", "exporter": "192.168.56.11:9100"},
    {"addr": "192.168.56.12", "name": "Backend-2", "exporter": "192.168.56.12:9100"},
    {"addr": "192.168.56.13", "name": "Backend-3", "exporter": "192.168.56.13:9100"},
]

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("autoscaler")


def get_cpu(server):
    """get cpu usage for one server from prometheus"""
    q = ('100 - (avg by(instance) '
         '(rate(node_cpu_seconds_total{instance="' + server["exporter"] + '",'
         'mode="idle"}[1m])) * 100)')
    try:
        r = requests.get(PROMETHEUS + "/api/v1/query", params={"query": q}, timeout=5)
        data = r.json()
        if data["status"] == "success" and data["data"]["result"]:
            return round(float(data["data"]["result"][0]["value"][1]), 2)
        return None
    except Exception as e:
        log.error("prometheus query failed for " + server["name"] + ": " + str(e))
        return None


def get_all_cpu(active):
    """get cpu for all active servers, returns dict"""
    cpus = {}
    for s in active:
        cpu = get_cpu(s)
        if cpu is not None:
            cpus[s["name"]] = cpu
            log.info("  " + s["name"] + " (" + s["addr"] + "): " + str(cpu) + "%")
        else:
            log.warning("  " + s["name"] + ": no data")
    return cpus


# nginx config stuff

def read_conf():
    with open(NGINX_CONF, "r") as f:
        return f.read()

def write_conf(config):
    with open(NGINX_CONF, "w") as f:
        f.write(config)

def reload_nginx():
    """test config then reload - returns True if ok"""
    try:
        test = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
        if test.returncode != 0:
            log.error("nginx config test failed: " + test.stderr)
            return False
        result = subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True, text=True)
        if result.returncode == 0:
            log.info("nginx reloaded ok")
            return True
        log.error("nginx reload failed: " + result.stderr)
        return False
    except Exception as e:
        log.error("reload error: " + str(e))
        return False


def find_upstream_block():
    """find the first upstream backend block in the config, return start/end line numbers"""
    config = read_conf()
    lines = config.split('\n')
    start = None
    end = None
    inside = False
    for i in range(len(lines)):
        s = lines[i].strip()
        if not inside and s.startswith('upstream backend'):
            start = i
            inside = True
        elif inside and s == '}':
            end = i
            break
    return start, end, lines


def get_active():
    """which servers are currently active (not commented out) in nginx config"""
    start, end, lines = find_upstream_block()
    if start is None:
        log.error("cant find upstream block!")
        return []
    active = []
    for i in range(start + 1, end):
        line = lines[i].strip()
        if not line or line.startswith('#'):
            continue
        for s in servers:
            if s['addr'] in line:
                active.append(s)
                break
    return active


def get_inactive():
    """servers that are commented out"""
    active_addrs = [s["addr"] for s in get_active()]
    return [s for s in servers if s["addr"] not in active_addrs]


def add_server(server):
    """uncomment a server line to add it to the pool"""
    start, end, lines = find_upstream_block()
    if start is None:
        return False
    for i in range(start + 1, end):
        if server['addr'] in lines[i] and lines[i].strip().startswith('#'):
            # remove the comment
            content = lines[i].strip().lstrip('#').strip()
            lines[i] = '    ' + content
            write_conf('\n'.join(lines))
            log.info("SCALE UP: added " + server["name"] + " (" + server["addr"] + ")")
            return True
    log.error("couldnt add " + server["name"])
    return False


def remove_server(server):
    """comment out a server line to remove it from pool"""
    start, end, lines = find_upstream_block()
    if start is None:
        return False
    for i in range(start + 1, end):
        s = lines[i].strip()
        if server['addr'] in s and not s.startswith('#'):
            lines[i] = '#   ' + s
            write_conf('\n'.join(lines))
            log.info("SCALE DOWN: removed " + server["name"] + " (" + server["addr"] + ")")
            return True
    log.error("couldnt remove " + server["name"])
    return False


def log_event(event_type, server):
    """save scaling event to json file for dissertation evidence"""
    event = {
        "timestamp": datetime.now().isoformat(),
        "event": event_type,
        "server": server["name"],
        "address": server["addr"],
        "active_after": [s["name"] for s in get_active()]
    }
    
    # load existing events or start fresh
    events = []
    if os.path.exists(EVENTS_LOG):
        try:
            with open(EVENTS_LOG, "r") as f:
                events = json.load(f)
        except:
            events = []
    
    events.append(event)
    with open(EVENTS_LOG, "w") as f:
        json.dump(events, f, indent=2)
    log.info("event logged: " + json.dumps(event))


def check_and_scale(cpu_data, active):
    """main scaling logic - decide whether to scale up, down, or do nothing"""
    if not cpu_data:
        log.warning("no cpu data, skipping")
        return
    
    avg = sum(cpu_data.values()) / len(cpu_data)
    inactive = get_inactive()
    
    log.info("  avg cpu: " + str(round(avg, 2)) + "% | active: " + str(len(active)) + 
             " | thresholds: up>" + str(SCALE_UP) + "% down<" + str(SCALE_DOWN) + "%")
    
    if avg > SCALE_UP and len(inactive) > 0:
        # cpu too high, add a server
        log.info("  >> SCALE UP (avg " + str(round(avg, 2)) + "% > " + str(SCALE_UP) + "%)")
        target = inactive[0]
        if add_server(target):
            reload_nginx()
            log_event("SCALE_UP", target)
    
    elif avg < SCALE_DOWN and len(active) > MIN_SERVERS:
        # cpu low enough to remove a server
        log.info("  >> SCALE DOWN (avg " + str(round(avg, 2)) + "% < " + str(SCALE_DOWN) + "%)")
        target = active[-1]
        if remove_server(target):
            reload_nginx()
            log_event("SCALE_DOWN", target)
    else:
        log.info("  >> no action needed")


# main loop
def main():
    log.info("VMSS Auto-Scaler starting...")
    log.info("scale up threshold: " + str(SCALE_UP) + "% | scale down: " + str(SCALE_DOWN) + "%")
    log.info("checking every " + str(CHECK_EVERY) + " seconds")
    
    cycle = 0
    try:
        while True:
            cycle += 1
            log.info("")
            log.info("--- cycle " + str(cycle) + " - " + datetime.now().strftime("%H:%M:%S") + " ---")
            
            active = get_active()
            log.info("active servers: " + str([s["name"] for s in active]))
            
            cpu_data = get_all_cpu(active)
            check_and_scale(cpu_data, active)
            
            time.sleep(CHECK_EVERY)
    
    except KeyboardInterrupt:
        log.info("\nstopped by user. active servers: " + str([s["name"] for s in get_active()]))


if __name__ == "__main__":
    main()
