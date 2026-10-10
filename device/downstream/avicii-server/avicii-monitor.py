#!/usr/bin/python3
"""Сборщик метрик для дашборда avicii-monitor.

Каждые SAMPLE_S секунд пишет в /run/avicii-monitor/ (tmpfs):
  now.json     — текущее состояние (питание, система, Wi-Fi, сервисы, Docker)
  events.json  — последние события
Каждые HIST_S секунд добавляет усреднённую точку в недельную историю
/var/lib/avicii-monitor/history.csv (на флэш, ~100 байт за запись) и обновляет history.json.

Для увеличения отрезков на графиках есть ещё два уровня детализации, нарезанные на файлы,
чтобы страница подгружала только нужный кусок:
  raw/<час>.csv   — каждый замер (5 с) за последние сутки, только в tmpfs, номер часа = t // 3600;
                    CSV и дозапись строкой, чтобы стоимость не росла к концу часа
  min/<сутки>.json — средние по минуте за неделю, номер суток = t // 86400;
                     источник — /var/lib/avicii-monitor/minute.csv (~100 байт в минуту)
"""

import glob
import http.client
import json
import os
import socket
import subprocess
import time
from collections import deque

SAMPLE_S = 5
HIST_S = 300
LIVE_N = 3600 // SAMPLE_S            # час
HIST_N = 7 * 24 * 3600 // HIST_S     # неделя
EVENTS_N = 300
SLOW_S = 30                          # Wi-Fi, сервисы, Docker, диск

RUN = "/run/avicii-monitor"
VAR = "/var/lib/avicii-monitor"
HIST_FILE = f"{VAR}/history.csv"
EVENTS_FILE = f"{VAR}/events.jsonl"
CHGLIMIT_LOG = "/var/log/avicii-chglimit.log"

BATT = "/sys/class/power_supply/battery"
USB = "/sys/class/power_supply/usb"
MMI = "/sys/class/oplus_chg/battery/mmi_charging_enable"

SERVICES = ["avicii-wlan", "avicii-chglimit", "avicii-monitor-web",
            "docker", "containerd", "dropbear", "networkmanager", "chronyd"]

SVC_RU = {"started": "работает", "stopped": "остановлен", "failed": "ошибка", "crashed": "упал"}
DOCKER_RU = {"running": "работает", "exited": "остановлен", "created": "создан", "paused": "на паузе",
             "restarting": "перезапуск", "dead": "мёртв", "removing": "удаляется"}

HIST_FIELDS = ["t", "p", "pmax", "cap", "tb", "tc", "load", "mem", "sig", "batt_s",
               "cpu", "cpumax", "iow", "mu", "mc"]

RAW_FIELDS = ["t", "p", "pu", "pb", "ib", "cap", "tb", "tc", "cable", "src", "mode",
              "load", "mem", "sig", "cpu", "iow", "mu", "mc", "f0", "f1", "f2"]
RAW_CHUNK = 3600
RAW_KEEP = 24 * 3600

MIN_S = 60
MIN_FILE = f"{VAR}/minute.csv"
MIN_CHUNK = 86400
MIN_KEEP = 7 * 24 * 3600


def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def rd_int(path, default=0):
    try:
        return int(rd(path))
    except (TypeError, ValueError):
        return default


def write_json(name, obj):
    d, base = os.path.split(f"{RUN}/{name}")
    tmp = f"{d}/.{base}.tmp"
    with open(tmp, "w") as f:
        # dumps, а не dump: dump в файл идёт через медленный кодировщик на чистом Python
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    os.chmod(tmp, 0o644)
    os.replace(tmp, f"{RUN}/{name}")


CLUSTER_NAMES = ["малые", "большое", "топовое"]


def cpu_clusters():
    res = []
    for i, p in enumerate(sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*"),
                                 key=lambda x: int(x.rsplit("policy", 1)[1]))):
        cpus = [int(c) for c in (rd(f"{p}/related_cpus", "") or "").split()]
        res.append({"path": p, "cpus": cpus,
                    "name": CLUSTER_NAMES[i] if i < len(CLUSTER_NAMES) else p.rsplit("/", 1)[1],
                    "min": rd_int(f"{p}/cpuinfo_min_freq") // 1000,
                    "max": rd_int(f"{p}/cpuinfo_max_freq") // 1000})
    return res


def proc_stat():
    """{"cpu": (busy, iowait, total), 0: (...), 1: (...)}"""
    res = {}
    for line in (rd("/proc/stat", "") or "").splitlines():
        if not line.startswith("cpu"):
            break
        f = line.split()
        v = [int(x) for x in f[1:9]]          # user nice system idle iowait irq softirq steal
        idle, iow = v[3], v[4]
        total = sum(v)
        key = "cpu" if f[0] == "cpu" else int(f[0][3:])
        res[key] = (total - idle - iow, iow, total)
    return res


def cpu_zones():
    zones = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = rd(f"{z}/type", "")
        if t.startswith("cpu-") and t.endswith("-usr"):
            zones.append(f"{z}/temp")
    return zones


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(self.unix_path)


def docker_containers():
    if not os.path.exists("/var/run/docker.sock"):
        return None
    try:
        c = UnixHTTP("/var/run/docker.sock")
        c.request("GET", "/containers/json?all=1")
        data = json.loads(c.getresponse().read())
        c.close()
    except (OSError, ValueError, http.client.HTTPException):
        return None
    return [{
        "name": (x.get("Names") or ["?"])[0].lstrip("/"),
        "image": x.get("Image", ""),
        "state": x.get("State", ""),
        "status": x.get("Status", ""),
    } for x in data]


def wifi():
    out = {"connected": False}
    try:
        link = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True,
                              text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in link.splitlines():
        line = line.strip()
        if line.startswith("SSID:"):
            out["ssid"] = line[5:].strip()
            out["connected"] = True
        elif line.startswith("freq:"):
            out["freq"] = round(float(line.split()[1]))
        elif line.startswith("signal:"):
            out["signal"] = int(line.split()[1])
        elif line.startswith("tx bitrate:"):
            out["tx"] = float(line.split()[2])
        elif line.startswith("rx bitrate:"):
            out["rx"] = float(line.split()[2])
    try:
        addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "wlan0"], capture_output=True,
                              text=True, timeout=3).stdout.split()
        out["ip"] = addr[addr.index("inet") + 1].split("/")[0]
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return out


def services():
    res = {}
    for s in SERVICES:
        if not os.path.exists(f"/etc/init.d/{s}"):
            continue
        if os.path.exists(f"/run/openrc/started/{s}"):
            res[s] = "started"
        elif os.path.exists(f"/run/openrc/failed/{s}"):
            res[s] = "failed"
        elif os.path.exists(f"/run/openrc/crashed/{s}"):
            res[s] = "crashed"
        else:
            res[s] = "stopped"
    return res


def meminfo():
    m = {}
    for line in (rd("/proc/meminfo", "") or "").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0])
    total, avail = m.get("MemTotal", 1), m.get("MemAvailable", 0)
    cache = m.get("Buffers", 0) + m.get("Cached", 0) + m.get("SReclaimable", 0) - m.get("Shmem", 0)
    return {"total_mb": total // 1024, "used_mb": (total - avail) // 1024,
            "cache_mb": max(0, cache) // 1024, "avail_mb": avail // 1024,
            "pct": round(100 * (total - avail) / total, 1)}


class Monitor:
    def __init__(self):
        os.makedirs(RUN, exist_ok=True)
        os.makedirs(VAR, exist_ok=True)
        os.chmod(RUN, 0o755)
        self.cpu_zones = cpu_zones()
        self.clusters = cpu_clusters()
        self.stat_prev = proc_stat()
        self.live = deque(maxlen=LIVE_N)
        self.hist = deque(maxlen=HIST_N)
        self.events = deque(maxlen=EVENTS_N)
        self.slow = {}
        self.slow_t = 0
        self.prev = {}
        self.chglimit_pos = None
        self.raw_key = None
        self.minutes = deque(maxlen=MIN_KEEP // MIN_S)
        self.min_key, self.min_rows = None, []
        self.load_history()
        self.load_raw()
        self.load_minutes()
        self.load_events()
        write_json("events.json", list(self.events))

    # --- события -------------------------------------------------------
    def event(self, kind, text, t=None):
        ev = {"t": int(t or time.time()), "kind": kind, "text": text}
        self.events.append(ev)
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if len(self.events) == EVENTS_N:
            # не даём файлу расти бесконечно
            with open(EVENTS_FILE + ".tmp", "w") as f:
                for e in self.events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(EVENTS_FILE + ".tmp", EVENTS_FILE)
        write_json("events.json", list(self.events))

    def load_events(self):
        for line in (rd(EVENTS_FILE, "") or "").splitlines():
            try:
                self.events.append(json.loads(line))
            except ValueError:
                pass

    def changed(self, key, value):
        old = self.prev.get(key, value)
        self.prev[key] = value
        return old != value, old

    def watch_chglimit(self):
        try:
            size = os.path.getsize(CHGLIMIT_LOG)
        except OSError:
            return
        if self.chglimit_pos is None or size < self.chglimit_pos:
            self.chglimit_pos = size
            return
        if size == self.chglimit_pos:
            return
        with open(CHGLIMIT_LOG) as f:
            f.seek(self.chglimit_pos)
            new = f.read()
            self.chglimit_pos = f.tell()
        for line in new.splitlines():
            parts = line.split(" ", 2)
            if len(parts) < 3:
                continue
            try:
                t = time.mktime(time.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                t = None
            self.event("charge", parts[2], t)

    # --- история -------------------------------------------------------
    def load_history(self):
        cutoff = time.time() - 7 * 24 * 3600
        lines = (rd(HIST_FILE, "") or "").splitlines()
        header = lines[0].split(",") if lines and lines[0].startswith("t,") else HIST_FIELDS
        for line in lines:
            vals = line.split(",")
            if len(vals) != len(header) or vals[0] == "t":
                continue
            try:
                got = dict(zip(header, (float(v) if v else None for v in vals)))
            except ValueError:
                continue
            row = {k: got.get(k) for k in HIST_FIELDS}
            if row["t"] >= cutoff:
                self.hist.append(row)
        if lines and header != HIST_FIELDS:
            # набор полей изменился — переписываем файл под новый заголовок, старые точки сохраняются
            self.rewrite_history()
        self.write_history()

    def rewrite_history(self):
        with open(HIST_FILE + ".tmp", "w") as f:
            f.write(",".join(HIST_FIELDS) + "\n")
            for r in self.hist:
                f.write(",".join("" if r.get(k) is None else str(r[k]) for k in HIST_FIELDS) + "\n")
        os.replace(HIST_FILE + ".tmp", HIST_FILE)

    def write_history(self):
        write_json("history.json", {"fields": HIST_FIELDS,
                                    "rows": [[r.get(k) for k in HIST_FIELDS] for r in self.hist]})

    def agg_row(self, now, span):
        pts = [s for s in self.live if s["t"] > now - span]
        if not pts:
            return None

        def avg(k):
            v = [p[k] for p in pts if p.get(k) is not None]
            return round(sum(v) / len(v), 2) if v else None

        return {"t": int(now), "p": avg("p"), "pmax": max(p["p"] for p in pts),
                "cap": pts[-1]["cap"], "tb": avg("tb"), "tc": avg("tc"),
                "load": avg("load"), "mem": avg("mem"), "sig": avg("sig"),
                # время без сети (кабель выдернут), а не режим drain, где вход отключён намеренно
                "batt_s": sum(SAMPLE_S for p in pts if p.get("cable") == 0),
                "cpu": avg("cpu"), "cpumax": max((p["cpu"] for p in pts if p.get("cpu") is not None), default=None),
                "iow": avg("iow"), "mu": avg("mu"), "mc": avg("mc")}

    def aggregate(self, now):
        row = self.agg_row(now, HIST_S)
        if not row:
            return
        self.hist.append(row)
        new_file = not os.path.exists(HIST_FILE)
        with open(HIST_FILE, "a") as f:
            if new_file:
                f.write(",".join(HIST_FIELDS) + "\n")
            f.write(",".join("" if row[k] is None else str(row[k]) for k in HIST_FIELDS) + "\n")
        # раз в сутки обрезаем файл до недели
        if int(now) // HIST_S % 288 == 0:
            self.rewrite_history()
        self.write_history()

    # --- уровни детализации для увеличения --------------------------------
    @staticmethod
    def chunk_json(sub, key, fields, rows):
        os.makedirs(f"{RUN}/{sub}", mode=0o755, exist_ok=True)
        write_json(f"{sub}/{key}.json", {"fields": fields, "rows": rows})

    @staticmethod
    def drop_chunks(sub, oldest_key):
        for f in glob.glob(f"{RUN}/{sub}/*.*"):
            try:
                if int(os.path.basename(f).split(".")[0]) < oldest_key:
                    os.remove(f)
            except ValueError:
                pass

    def load_raw(self):
        """После перезапуска сборщика восстанавливаем последний час замеров из сырых файлов."""
        now = time.time()
        for key in (int(now) // RAW_CHUNK - 1, int(now) // RAW_CHUNK):
            lines = (rd(f"{RUN}/raw/{key}.csv", "") or "").splitlines()
            if not lines or lines[0].split(",") != RAW_FIELDS:
                continue
            for line in lines[1:]:
                row = {}
                for k, v in zip(RAW_FIELDS, line.split(",")):
                    try:
                        row[k] = float(v) if v else None
                    except ValueError:
                        row[k] = v
                if row.get("t") and row["t"] > now - 3600:
                    self.live.append(row)

    def add_raw(self, s):
        key = s["t"] // RAW_CHUNK
        if key != self.raw_key:
            self.raw_key = key
            self.drop_chunks("raw", (s["t"] - RAW_KEEP) // RAW_CHUNK)
        f = s.get("f") or []
        row = dict(s, f0=f[0] if len(f) > 0 else None, f1=f[1] if len(f) > 1 else None,
                   f2=f[2] if len(f) > 2 else None)
        os.makedirs(f"{RUN}/raw", mode=0o755, exist_ok=True)
        path = f"{RUN}/raw/{key}.csv"
        new = not os.path.exists(path)
        with open(path, "a") as fh:
            if new:
                os.fchmod(fh.fileno(), 0o644)
                fh.write(",".join(RAW_FIELDS) + "\n")
            fh.write(",".join("" if row.get(k) is None else str(row[k]) for k in RAW_FIELDS) + "\n")

    def load_minutes(self):
        cutoff = time.time() - MIN_KEEP
        lines = (rd(MIN_FILE, "") or "").splitlines()
        header = lines[0].split(",") if lines and lines[0].startswith("t,") else HIST_FIELDS
        days = {}
        for line in lines[1:]:
            vals = line.split(",")
            if len(vals) != len(header):
                continue
            try:
                got = dict(zip(header, (float(v) if v else None for v in vals)))
            except ValueError:
                continue
            if got["t"] < cutoff:
                continue
            row = {k: got.get(k) for k in HIST_FIELDS}
            self.minutes.append(row)
            days.setdefault(int(row["t"]) // MIN_CHUNK, []).append([row[k] for k in HIST_FIELDS])
        for key, rows in days.items():
            self.chunk_json("min", key, HIST_FIELDS, rows)
        self.min_key = int(time.time()) // MIN_CHUNK
        self.min_rows = days.get(self.min_key, [])
        if lines and header != HIST_FIELDS:
            self.rewrite_minutes()

    def rewrite_minutes(self):
        with open(MIN_FILE + ".tmp", "w") as f:
            f.write(",".join(HIST_FIELDS) + "\n")
            for r in self.minutes:
                f.write(",".join("" if r.get(k) is None else str(r[k]) for k in HIST_FIELDS) + "\n")
        os.replace(MIN_FILE + ".tmp", MIN_FILE)

    def add_minute(self, now):
        row = self.agg_row(now, MIN_S)
        if not row:
            return
        self.minutes.append(row)
        new_file = not os.path.exists(MIN_FILE)
        with open(MIN_FILE, "a") as f:
            if new_file:
                f.write(",".join(HIST_FIELDS) + "\n")
            f.write(",".join("" if row[k] is None else str(row[k]) for k in HIST_FIELDS) + "\n")
        key = row["t"] // MIN_CHUNK
        if key != self.min_key:
            self.min_key, self.min_rows = key, []
            self.drop_chunks("min", (row["t"] - MIN_KEEP) // MIN_CHUNK)
            self.rewrite_minutes()      # раз в сутки обрезаем файл до недели
        self.min_rows.append([row[k] for k in HIST_FIELDS])
        self.chunk_json("min", key, HIST_FIELDS, self.min_rows)

    # --- замер ---------------------------------------------------------
    def sample(self, now):
        online = rd_int(f"{USB}/online")
        present = rd_int(f"{USB}/present")
        uv, ui = rd_int(f"{USB}/voltage_now"), rd_int(f"{USB}/input_current_now")
        bv, bi = rd_int(f"{BATT}/voltage_now"), rd_int(f"{BATT}/current_now")
        if online != 1:     # при отключённом входе датчик USB показывает мусор
            uv = ui = 0
        pu = uv / 1e6 * ui / 1e6
        pb = bv / 1e6 * bi / 1e3
        p = pu + pb
        susp = rd_int(f"{BATT}/input_suspend")
        mmi = rd_int(MMI, 1)
        if susp:
            mode = "drain"
        elif mmi == 0:
            mode = "hold"
        else:
            mode = "charge"
        src = "usb" if online == 1 else "batt"
        temps = [rd_int(z, 0) for z in self.cpu_zones]
        load = float((rd("/proc/loadavg", "0") or "0").split()[0])
        mem = meminfo()
        cpu = self.cpu_sample()
        s = {
            "t": int(now), "p": round(p, 3), "pu": round(pu, 3), "pb": round(pb, 3),
            "ib": bi, "i": round(p / (bv / 1e6) * 1e3) if bv else 0,
            "cap": rd_int(f"{BATT}/capacity"), "tb": rd_int(f"{BATT}/temp") / 10,
            "tc": round(max(temps) / 1000, 1) if temps else None,
            "src": src, "mode": mode, "cable": present, "load": load, "mem": mem["pct"],
            "sig": self.slow.get("wifi", {}).get("signal"),
            "cpu": cpu["busy"], "iow": cpu["iow"],
            "cl": [c["busy"] for c in cpu["clusters"]], "f": [c["cur"] for c in cpu["clusters"]],
            "mu": mem["used_mb"], "mc": mem["cache_mb"],
        }
        return s, mem, cpu

    def cpu_sample(self):
        cur = proc_stat()
        prev, self.stat_prev = self.stat_prev, cur

        def busy(keys):
            db = sum(cur[k][0] - prev[k][0] for k in keys if k in cur and k in prev)
            dt = sum(cur[k][2] - prev[k][2] for k in keys if k in cur and k in prev)
            return round(100 * db / dt, 1) if dt > 0 else 0.0

        dt = cur["cpu"][2] - prev["cpu"][2]
        iow = round(100 * (cur["cpu"][1] - prev["cpu"][1]) / dt, 1) if dt > 0 else 0.0
        clusters = [{"name": c["name"], "cpus": c["cpus"], "min": c["min"], "max": c["max"],
                     "cur": rd_int(f"{c['path']}/scaling_cur_freq") // 1000,
                     "gov": rd(f"{c['path']}/scaling_governor", ""),
                     "busy": busy(c["cpus"])} for c in self.clusters]
        return {"busy": busy(["cpu"]), "iow": iow, "clusters": clusters, "ncpu": len(cur) - 1}

    def slow_update(self, now):
        self.slow["wifi"] = wifi()
        self.slow["services"] = services()
        self.slow["docker"] = docker_containers()
        st = os.statvfs("/")
        self.slow["disk"] = {"total_gb": round(st.f_blocks * st.f_frsize / 2**30, 1),
                             "used_gb": round((st.f_blocks - st.f_bfree) * st.f_frsize / 2**30, 1)}
        self.slow_t = now

        w = self.slow["wifi"]
        ch, _ = self.changed("wifi", w.get("connected"))
        if ch:
            self.event("wifi", f"Wi-Fi подключён к {w.get('ssid', '?')}" if w.get("connected")
                       else "Wi-Fi отключился")
        for name, state in self.slow["services"].items():
            ch, old = self.changed(f"svc:{name}", state)
            if ch:
                self.event("service", f"Сервис {name}: {SVC_RU.get(old, old)} → {SVC_RU.get(state, state)}")
        for c in self.slow["docker"] or []:
            ch, old = self.changed(f"ctr:{c['name']}", c["state"])
            if ch:
                self.event("docker", f"Контейнер {c['name']}: {DOCKER_RU.get(old, old)} → "
                                     f"{DOCKER_RU.get(c['state'], c['state'])}")

    def tick(self):
        now = time.time()
        if now - self.slow_t >= SLOW_S:
            self.slow_update(now)
        s, mem, cpu = self.sample(now)
        self.live.append(s)
        self.add_raw(s)

        ch, _ = self.changed("cable", s["cable"])
        if ch:
            self.event("power", "Питание от сети восстановлено" if s["cable"]
                       else "Питание от сети пропало — работа от батареи")
        hot = s["tb"] >= 40 or (self.prev.get("hot") and s["tb"] >= 38)
        ch, _ = self.changed("hot", hot)
        if ch:
            self.event("temp", f"Батарея нагрелась до {s['tb']:.1f} °C" if hot
                       else f"Температура батареи в норме ({s['tb']:.1f} °C)")
        self.watch_chglimit()

        up = float((rd("/proc/uptime", "0") or "0").split()[0])
        write_json("now.json", {
            "t": s["t"], "power": s, "mem": mem, "cpu": cpu, "uptime": int(up),
            "loadavg": (rd("/proc/loadavg", "") or "").split()[:3],
            "cycles": rd_int(f"{BATT}/cycle_count"), "health": rd(f"{BATT}/health"),
            "chglimit": self.chglimit_conf(), "sample_s": SAMPLE_S,
            "tiers": {"raw": {"step": SAMPLE_S, "chunk": RAW_CHUNK, "keep": RAW_KEEP},
                      "min": {"step": MIN_S, "chunk": MIN_CHUNK, "keep": MIN_KEEP},
                      "hist": {"step": HIST_S, "keep": HIST_N * HIST_S}},
            **self.slow,
        })
        if int(now) // MIN_S != int(now - SAMPLE_S) // MIN_S:
            self.add_minute(now)
        if int(now) // HIST_S != int(now - SAMPLE_S) // HIST_S:
            self.aggregate(now)

    def chglimit_conf(self):
        conf = {}
        for line in (rd("/etc/conf.d/avicii-chglimit", "") or "").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"')
        return {"max": int(conf.get("CHG_MAX", 80)), "min": int(conf.get("CHG_MIN", 70))}

    def run(self):
        boot = time.time() - float((rd("/proc/uptime", "0") or "0").split()[0])
        if not os.path.exists(f"{RUN}/.boot-seen"):
            self.event("boot", "Загрузка системы", boot)
            open(f"{RUN}/.boot-seen", "w").close()
        while True:
            start = time.time()
            try:
                self.tick()
            except Exception as e:      # сборщик не должен падать из-за одного сбойного датчика
                print(f"{time.strftime('%F %T')} ошибка замера: {e!r}", flush=True)
            time.sleep(max(0.5, SAMPLE_S - (time.time() - start)))


if __name__ == "__main__":
    Monitor().run()
