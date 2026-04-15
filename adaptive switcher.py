import subprocess
import requests
import time
import json
import os
import logging
from datetime import datetime

PROMETHEUS = "http://localhost:9090"
NGINX_CONF = "/etc/nginx/sites-enabled/default"
LOG_FILE = "/var/log/nginx/adaptive_switcher.log"
EVENTS_LOG = "/var/log/nginx/adaptive_switch_events.json"

LOW_THRESHOLD = 40.0     
HIGH_THRESHOLD = 70.0    
IMBALANCE_LIMIT = 30.0   

CHECK_EVERY = 15  

backends = [
    {"addr": "192.168.56.11", "name": "Backend-1", "exporter": "192.168.56.11:9100"},
    {"addr": "192.168.56.12", "name": "Backend-2", "exporter": "192.168.56.12:9100"},
    {"addr": "192.168.56.13", "name": "Backend-3", "exporter": "192.168.56.13:9100"},
]

algo_configs = {
    "round_robin": {
        "label": "Round Robin",
        "why": "low load - simple distribution is fine",
        "block": """upstream backend {{
    server 192.168.56.11 max_fails=3 fail_timeout=30s;
    server 192.168.56.12 max_fails=3 fail_timeout=30s;
    server 192.168.56.13 max_fails=3 fail_timeout=30s;
}}"""
    },
    "weighted_rr": {
        "label": "Weighted Round Robin",
        "why": "medium load - distribute by capacity",
        "block": """upstream backend {{
    server 192.168.56.11 weight=7 max_fails=3 fail_timeout=30s;
    server 192.168.56.12 weight=3 max_fails=3 fail_timeout=30s;
    server 192.168.56.13 weight=1 max_fails=3 fail_timeout=30s;
}}"""
    },
    "least_conn": {
        "label": "Least Connections",
        "why": "high/uneven load - route to least busy server",
        "block": """upstream backend {{
    least_conn;
    server 192.168.56.11 max_fails=3 fail_timeout=30s;
    server 192.168.56.12 max_fails=3 fail_timeout=30s;
    server 192.168.56.13 max_fails=3 fail_timeout=30s;
}}"""
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("switcher")

# prometheus config 
def get_cpu(server):
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
        log.error("query failed for " + server["name"] + ": " + str(e))
        return None

def get_mem(server):
    """memory usage - not used for switching but logged for evidence"""
    q = ('100 - ((node_memory_MemAvailable_bytes{instance="' + server["exporter"] + '"} / '
         'node_memory_MemTotal_bytes{instance="' + server["exporter"] + '"}) * 100)')
    try:
        r = requests.get(PROMETHEUS + "/api/v1/query", params={"query": q}, timeout=5)
        data = r.json()
        if data["status"] == "success" and data["data"]["result"]:
            return round(float(data["data"]["result"][0]["value"][1]), 2)
        return None
    except:
        return None

def get_all_metrics():
    """grab cpu and memory for every backend"""
    metrics = {}
    for srv in backends:
        cpu = get_cpu(srv)
        mem = get_mem(srv)
        metrics[srv["name"]] = {"addr": srv["addr"], "cpu": cpu, "mem": mem}
        if cpu is not None:
            log.info("  " + srv["name"] + ": cpu=" + str(cpu) + "% mem=" + str(mem) + "%")
        else:
            log.warning("  " + srv["name"] + ": no data")
    return metrics

# Nginx config management
def read_conf():
    with open(NGINX_CONF) as f:
        return f.read()

def write_conf(config):
    with open(NGINX_CONF, "w") as f:
        f.write(config)

def reload_nginx():
    try:
        test = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
        if test.returncode != 0:
            log.error("config test failed: " + test.stderr)
            return False
        res = subprocess.run(["sudo", "systemctl", "reload", "nginx"], capture_output=True, text=True)
        if res.returncode == 0:
            log.info("nginx reloaded")
            return True
        log.error("reload failed")
        return False
    except Exception as e:
        log.error("error: " + str(e))
        return False


def detect_current_algo():
    """figure out which algorithm is currently active by reading the config"""
    config = read_conf()
    lines = config.split('\n')
    inside = False
    for line in lines:
        s = line.strip()
        if s.startswith('upstream backend') and not s.startswith('#'):
            inside = True
            continue
        if inside:
            if s == '}':
                break
            if s.startswith('#'):
                continue
            if 'least_conn' in s:
                return "least_conn"
            if 'ip_hash' in s:
                return "ip_hash"
            if 'weight=' in s:
                return "weighted_rr"
    return "round_robin"


def switch_to(new_algo):
    """replace the upstream block with the new algorithm's config"""
    config = read_conf()
    lines = config.split('\n')
    
    start = end = None
    inside = False
    for i in range(len(lines)):
        s = lines[i].strip()
        if not inside and s.startswith('upstream backend') and not s.startswith('#'):
            start = i
            inside = True
        elif inside and s == '}':
            end = i
            break
    
    if start is None or end is None:
        log.error("cant find upstream block!")
        return False
    
    new_block = algo_configs[new_algo]["block"].split('\n')
    new_lines = lines[:start] + new_block + lines[end + 1:]
    write_conf('\n'.join(new_lines))
    
    log.info("switched to " + algo_configs[new_algo]["label"])
    return reload_nginx()

def pick_algorithm(metrics):
    """look at current cpu across all servers and decide which algorithm to use"""
    cpus = []
    for name in metrics:
        cpu = metrics[name]["cpu"]
        if cpu is not None:
            cpus.append(cpu)
    
    if not cpus:
        log.warning("no cpu data - keeping current algo")
        return None
    
    avg = sum(cpus) / len(cpus)
    spread = max(cpus) - min(cpus)
    
    log.info("  avg: " + str(round(avg, 2)) + "% | max: " + str(max(cpus)) + 
             "% | min: " + str(min(cpus)) + "% | spread: " + str(round(spread, 2)) + "%")
    
    if spread > IMBALANCE_LIMIT:
        log.info("  -> high imbalance (" + str(round(spread, 2)) + "% spread)")
        return "least_conn"
    
    # otherwise decide by average load
    if avg > HIGH_THRESHOLD:
        log.info("  -> high load (" + str(round(avg, 2)) + "%)")
        return "least_conn"
    elif avg > LOW_THRESHOLD:
        log.info("  -> medium load (" + str(round(avg, 2)) + "%)")
        return "weighted_rr"
    else:
        log.info("  -> low load (" + str(round(avg, 2)) + "%)")
        return "round_robin"


def log_switch(old, new, metrics, reason):
    """save the switch to json for dissertation"""
    cpu_vals = {}
    for name in metrics:
        cpu_vals[name] = metrics[name]["cpu"]
    
    valid_cpus = [v for v in cpu_vals.values() if v is not None]
    
    event = {
        "timestamp": datetime.now().isoformat(),
        "event": "SWITCH",
        "from": old,
        "to": new,
        "reason": reason,
        "cpus": cpu_vals,
        "avg_cpu": round(sum(valid_cpus) / len(valid_cpus), 2) if valid_cpus else 0
    }
    
    events = []
    if os.path.exists(EVENTS_LOG):
        try:
            with open(EVENTS_LOG) as f:
                events = json.load(f)
        except:
            events = []
    
    events.append(event)
    with open(EVENTS_LOG, "w") as f:
        json.dump(events, f, indent=2)
    log.info("switch logged: " + old + " -> " + new)


def main():
    log.info("Adaptive Switcher starting...")
    log.info("thresholds: low<" + str(LOW_THRESHOLD) + "% medium=" + str(LOW_THRESHOLD) + 
             "-" + str(HIGH_THRESHOLD) + "% high>" + str(HIGH_THRESHOLD) + "%")
    log.info("imbalance trigger: " + str(IMBALANCE_LIMIT) + "% spread")
    
    cycle = 0
    try:
        while True:
            cycle += 1
            log.info("")
            log.info("--- cycle " + str(cycle) + " - " + datetime.now().strftime("%H:%M:%S") + " ---")
            
            current = detect_current_algo()
            log.info("current algo: " + algo_configs.get(current, {}).get("label", current))
            
            log.info("querying prometheus...")
            metrics = get_all_metrics()
            
            recommended = pick_algorithm(metrics)
            
            if recommended is None:
                log.info("  >> keeping current (no data)")
            elif recommended == current:
                log.info("  >> keeping " + algo_configs[current]["label"] + " (already right)")
            else:
                log.info("  >> SWITCHING: " + algo_configs[current]["label"] + 
                        " -> " + algo_configs[recommended]["label"])
                log.info("  >> reason: " + algo_configs[recommended]["why"])
                
                if switch_to(recommended):
                    log_switch(current, recommended, metrics, algo_configs[recommended]["why"])
                    log.info("  >> done!")
                else:
                    log.error("  >> switch FAILED")
            
            time.sleep(CHECK_EVERY)
    
    except KeyboardInterrupt:
        final = detect_current_algo()
        log.info("\nstopped. final algo: " + algo_configs.get(final, {}).get("label", final))


if __name__ == "__main__":
    main()
