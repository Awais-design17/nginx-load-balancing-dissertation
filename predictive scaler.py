import subprocess
import requests
import time
import json
import os
import logging
from datetime import datetime

PROMETHEUS = "http://localhost:9090"
NGINX_CONF = "/etc/nginx/sites-enabled/default"
LOG_FILE = "/var/log/nginx/predictive_scaler.log"
EVENTS_LOG = "/var/log/nginx/predictive_scaling_events.json"

SCALE_UP = 75.0
SCALE_DOWN = 30.0
CHECK_EVERY = 10
MIN_SERVERS = 1

LOOKBACK = 5      
PREDICT_AHEAD = 2  
STEP = 10         

servers = [
    {"addr": "192.168.56.11", "name": "Backend-1", "exporter": "192.168.56.11:9100"},
    {"addr": "192.168.56.12", "name": "Backend-2", "exporter": "192.168.56.12:9100"},
    {"addr": "192.168.56.13", "name": "Backend-3", "exporter": "192.168.56.13:9100"},
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger("predictive")


def do_regression(x_vals, y_vals):
    n = len(x_vals)
    if n < 2:
        return 0.0, y_vals[0] if y_vals else 0.0
    
    sx = sum(x_vals)
    sy = sum(y_vals)
    sxy = sum(x * y for x, y in zip(x_vals, y_vals))
    sx2 = sum(x * x for x in x_vals)
    
    denom = n * sx2 - sx * sx
    if denom == 0:
        return 0.0, sy / n
    
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def predict_value(slope, intercept, last_x, seconds_ahead):
    """project the trend line forward"""
    future_x = last_x + seconds_ahead
    pred = slope * future_x + intercept
    return max(0.0, min(100.0, pred))


def get_cpu_history(server):
    """get last LOOKBACK minutes of cpu data from prometheus range query"""
    q = ('100 - (avg by(instance) '
         '(rate(node_cpu_seconds_total{instance="' + server["exporter"] + '",'
         'mode="idle"}[1m])) * 100)')
    
    now = time.time()
    start = now - (LOOKBACK * 60)
    
    try:
        r = requests.get(PROMETHEUS + "/api/v1/query_range",
            params={"query": q, "start": start, "end": now, "step": STEP},
            timeout=10)
        data = r.json()
        if data["status"] == "success" and data["data"]["result"]:
            vals = data["data"]["result"][0]["values"]
            history = []
            for ts, val in vals:
                cpu = float(val)
                if cpu == cpu:  
                    history.append((float(ts), cpu))
            return history
        return []
    except Exception as e:
        log.error("history query failed for " + server["name"] + ": " + str(e))
        return []


def get_current_cpu(server):
    """just get the current cpu value"""
    q = ('100 - (avg by(instance) '
         '(rate(node_cpu_seconds_total{instance="' + server["exporter"] + '",'
         'mode="idle"}[1m])) * 100)')
    try:
        r = requests.get(PROMETHEUS + "/api/v1/query", params={"query": q}, timeout=5)
        data = r.json()
        if data["status"] == "success" and data["data"]["result"]:
            return round(float(data["data"]["result"][0]["value"][1]), 2)
        return None
    except:
        return None

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
        log.error("reload error: " + str(e))
        return False

def find_upstream():
    config = read_conf()
    lines = config.split('\n')
    start = end = None
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
    start, end, lines = find_upstream()
    if start is None:
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
    active_addrs = [s["addr"] for s in get_active()]
    return [s for s in servers if s["addr"] not in active_addrs]

def add_server(server):
    start, end, lines = find_upstream()
    if start is None:
        return False
    for i in range(start + 1, end):
        if server['addr'] in lines[i] and lines[i].strip().startswith('#'):
            content = lines[i].strip().lstrip('#').strip()
            lines[i] = '    ' + content
            write_conf('\n'.join(lines))
            log.info("PREDICTIVE SCALE UP: added " + server["name"])
            return True
    return False

def remove_server(server):
    start, end, lines = find_upstream()
    if start is None:
        return False
    for i in range(start + 1, end):
        s = lines[i].strip()
        if server['addr'] in s and not s.startswith('#'):
            lines[i] = '#   ' + s
            write_conf('\n'.join(lines))
            log.info("PREDICTIVE SCALE DOWN: removed " + server["name"])
            return True
    return False


def analyse_trends(active):
    """look at cpu history for each server, run regression, predict future"""
    all_current = []
    all_predicted = []
    details = {}
    
    for srv in active:
        history = get_cpu_history(srv)
        current = get_current_cpu(srv)
        
        if not history or len(history) < 3:
            log.warning("  " + srv["name"] + ": not enough data points for prediction")
            if current is not None:
                all_current.append(current)
            details[srv["name"]] = {"current": current, "predicted": None, "trend": "unknown"}
            continue
        
        x = [p[0] for p in history]
        y = [p[1] for p in history]
        
        t0 = x[0]
        x_norm = [xi - t0 for xi in x]
        
      
        slope, intercept = do_regression(x_norm, y)
        
        last_t = x_norm[-1]
        future_secs = PREDICT_AHEAD * 60
        predicted = predict_value(slope, intercept, last_t, future_secs)
        
        if slope > 0.05:
            trend = "rising"
        elif slope < -0.05:
            trend = "falling"
        else:
            trend = "stable"
        
        rate_per_min = slope * 60  
        
        if current is not None:
            all_current.append(current)
        all_predicted.append(predicted)
        
        details[srv["name"]] = {
            "current": current,
            "predicted": round(predicted, 2),
            "trend": trend,
            "rate_per_min": round(rate_per_min, 2),
            "points": len(history)
        }
        
        log.info("  " + srv["name"] + ": now=" + str(current) + "% predicted=" + 
                 str(round(predicted, 2)) + "% trend=" + trend + 
                 " (" + str(round(rate_per_min, 2)) + "%/min)")
    
    curr_avg = sum(all_current) / len(all_current) if all_current else 0
    pred_avg = sum(all_predicted) / len(all_predicted) if all_predicted else curr_avg
    
    overall = "stable"
    if pred_avg > curr_avg + 2:
        overall = "rising"
    elif pred_avg < curr_avg - 2:
        overall = "falling"
    
    return {
        "current_avg": round(curr_avg, 2),
        "predicted_avg": round(pred_avg, 2),
        "trend": overall,
        "details": details
    }


def decide_action(analysis, active):
    """the key bit - decide based on PREDICTED cpu, not just current"""
    current = analysis["current_avg"]
    predicted = analysis["predicted_avg"]
    inactive = get_inactive()
    
    log.info("  current avg: " + str(current) + "% | predicted avg: " + str(predicted) + 
             "% | trend: " + analysis["trend"])
    
    if predicted > SCALE_UP and len(inactive) > 0:
        if current < SCALE_UP:
            log.info("  >> PREDICTIVE SCALE UP: cpu at " + str(current) + 
                    "% but predicted " + str(predicted) + "% in " + str(PREDICT_AHEAD) + "min")
            return "predictive_up"
        else:
            log.info("  >> REACTIVE SCALE UP: already at " + str(current) + "%")
            return "reactive_up"
    
    elif predicted < SCALE_DOWN and len(active) > MIN_SERVERS:
        if current > SCALE_DOWN:
            log.info("  >> PREDICTIVE SCALE DOWN: cpu at " + str(current) +
                    "% but predicted " + str(predicted) + "% in " + str(PREDICT_AHEAD) + "min")
            return "predictive_down"
        else:
            log.info("  >> REACTIVE SCALE DOWN: already at " + str(current) + "%")
            return "reactive_down"
    
    else:
        log.info("  >> no action (predicted " + str(predicted) + "% is in safe range)")
        return "none"


def do_scaling(action):
    """actually perform the scaling"""
    if "up" in action:
        inactive = get_inactive()
        if inactive:
            srv = inactive[0]
            if add_server(srv):
                reload_nginx()
                return srv
    elif "down" in action:
        active = get_active()
        if len(active) > MIN_SERVERS:
            srv = active[-1]
            if remove_server(srv):
                reload_nginx()
                return srv
    return None


def log_event(cycle, analysis, action, scaled):
    """save everything to json for the dissertation"""
    event = {
        "timestamp": datetime.now().isoformat(),
        "cycle": cycle,
        "current_avg": analysis["current_avg"],
        "predicted_avg": analysis["predicted_avg"],
        "trend": analysis["trend"],
        "action": action,
        "type": "predictive" if "predictive" in action else "reactive" if "reactive" in action else "none",
        "scaled_server": scaled["name"] if scaled else None,
        "active_servers": [s["name"] for s in get_active()],
        "server_details": analysis["details"]
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


def main():
    log.info("Predictive auto-scaler starting...")
    log.info("lookback: " + str(LOOKBACK) + "min | predict ahead: " + str(PREDICT_AHEAD) + "min")
    log.info("thresholds: up>" + str(SCALE_UP) + "% down<" + str(SCALE_DOWN) + "%")
    
    cycle = 0
    try:
        while True:
            cycle += 1
            log.info("")
            log.info("--- prediction cycle " + str(cycle) + " - " + datetime.now().strftime("%H:%M:%S") + " ---")
            
            active = get_active()
            log.info("active: " + str([s["name"] for s in active]))
            
            if not active:
                log.warning("no active servers!")
                time.sleep(CHECK_EVERY)
                continue
            analysis = analyse_trends(active)
            action = decide_action(analysis, active)
            scaled = None
            if action != "none":
                scaled = do_scaling(action)
                if scaled:
                    log.info("updated active: " + str([s["name"] for s in get_active()]))
            log_event(cycle, analysis, action, scaled)
            time.sleep(CHECK_EVERY)
    except KeyboardInterrupt:
        log.info("\nstopped. final active: " + str([s["name"] for s in get_active()]))

if __name__ == "__main__":
    main()
