# nginx-load-balancing-dissertation
MSc Dissertation - Comparative Analysis of Nginx Load Balancing Algorithms with Adaptive and Predictive Scaling.
**University of Roehampton | MSc Computing | 2026**
**Supervisor: Kashif**

---

## What This Project Is About

This dissertation investigates how the five built-in Nginx load balancing algorithms compare against each other when tested on real infrastructure — not simulation. It also explores whether cloud-style auto-scaling and adaptive algorithm switching can be achieved using only open-source tools, without any commercial software.

The five algorithms tested are:
- Round Robin
- Weighted Round Robin
- Least Connections
- IP Hash
- Consistent Hashing

On top of the algorithm comparison, three Python-based adaptive systems were built and tested:
- A **reactive auto-scaler** that adds or removes backend servers based on CPU thresholds
- A **predictive auto-scaler** that uses linear regression to forecast CPU load and scale before a threshold is reached
- An **adaptive algorithm switcher** that automatically changes the load balancing algorithm based on real-time server load

---

## Key Results

| Algorithm | Throughput | Avg Response Time | Error Rate |
|---|---|---|---|
| Consistent Hashing | 43.2 req/s | 1,433 ms | 0% |
| Least Connections | 42.5 req/s | 1,490 ms | 0% |
| Weighted Round Robin | 41.8 req/s | 1,544 ms | 0% |
| Round Robin | 41.7 req/s | 1,540 ms | 0% |
| IP Hash | 40.5 req/s | 1,579 ms | 0% |

- The predictive scaler triggered **15 to 20 seconds earlier** than the reactive scaler
- The adaptive switcher worked correctly across **524 monitoring cycles**
- Server failure testing confirmed **automatic failover with zero dropped requests**
- Wireshark independently verified all traffic distributions at the network level

---

## Infrastructure Setup

All four virtual machines ran on Oracle VirtualBox 7.0 on a Windows 11 host.

| Machine | Role | IP Address | Software |
|---|---|---|---|
| Dessertation | Load Balancer | 192.168.1.120 | Nginx 1.24.0, Prometheus, Grafana, GoAccess |
| Backend-1 | Web Server | 192.168.56.11 | Apache2, Node Exporter |
| Backend-2 | Web Server | 192.168.56.12 | Apache2, Node Exporter |
| Backend-3 | Web Server | 192.168.56.13 | Apache2, Node Exporter |

All backends used Ubuntu 24.04 LTS with 2 vCPU and 2 GB RAM. The backends were isolated from external networks so that packet captures and timing measurements were not affected by background traffic.

---

## Tools Used

| Tool | Version | Purpose |
|---|---|---|
| Nginx | 1.24.0 | Load balancer and reverse proxy |
| Apache JMeter | 5.6.3 | HTTPS load testing at 100 concurrent users |
| Prometheus | 2.45.0 | CPU and memory metric collection |
| Grafana | 10.2.3 | Real-time dashboard visualisation |
| GoAccess | 1.8.1 | Nginx log analysis and request counting |
| Wireshark / tcpdump | Latest | Network-layer packet distribution verification |
| Python | 3.12 | Adaptive auto-scaling and algorithm switching |
| Oracle VirtualBox | 7.0 | Virtualisation platform |

---

## Repository Structure

```
nginx-load-balancing-dissertation/
│
├── nginx-configs/
│   ├── roundrobin.conf           # Round Robin upstream block
│   ├── weightedRR.conf           # Weighted Round Robin with 5:3:2 weights
│   ├── leastconn.conf            # Least Connections
│   ├── iphash.conf               # IP Hash
│   └── consistenthash.conf       # Consistent Hashing (URI-based)
│
├── python-scripts/
│   ├── vmss_autoscaler.py        # Reactive auto-scaler (75% / 30% thresholds)
│   ├── predictive_scaler.py      # Predictive scaler (linear regression)
│   └── adaptive_switcher.py      # Algorithm switcher (40% / 70% thresholds)
│
├── prometheus/
│   └── prometheus.yml            # Prometheus config with 3 backend scrape targets
│
├── jmeter/
│   └── loadtest.jmx              # JMeter test plan (100 threads, 10s ramp, 1000 requests)
│
├── results/
│   ├── vmss_scaling_events.json       # Reactive scaler event log
│   ├── predictive_scaling_events.json # Predictive scaler event log
│   ├── adaptive_switcher.log          # Algorithm switching log
│   └── goaccess_report.html           # GoAccess report confirming 6,000 requests, 0 failures
│
└── README.md
```

---

## How the Testing Was Done

1. Each algorithm was configured in the Nginx upstream block one at a time
2. The configuration was validated with `sudo nginx -t` and reloaded with `sudo systemctl reload nginx`
3. 30 curl requests were sent first to verify routing behaviour
4. Apache JMeter was then run from the Windows host with 100 concurrent threads, 10-second ramp-up, and 10 iterations per thread (1,000 HTTPS requests total)
5. tcpdump captured packets on interface enp0s3 during the curl test
6. The .pcap file was analysed in Wireshark using the Conversations IPv4 view to count packets per backend independently of JMeter

---

## What Makes This Project Different

Most published Nginx load balancing studies either:
- Only test 2 or 3 algorithms
- Use simulation tools instead of real infrastructure
- Do not verify traffic distribution at the network level

This project tests all five standard Nginx algorithms simultaneously on real virtual machines with HTTPS traffic, and independently verifies every distribution result using both JMeter (application layer) and Wireshark (network layer). To the best of the author's knowledge, no prior published study has done all three of these things together in a single standard Nginx deployment.

---

## Contact

**Student:** Awais
**University:** University of Roehampton
**Degree:** MSc Computing
**Year:** 2026
