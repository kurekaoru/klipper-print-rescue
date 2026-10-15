#!/usr/bin/env python3
"""printrescue -- continue a failed FDM print from the layer where it stopped, with the part
still on the bed. Klipper + Moonraker; developed on a Creality Ender-3 V3 Plus (Creality's
Klipper fork with a load-cell bed probe), should work on any Klipper printer whose `PROBE`
can touch the top of a printed part.

Every command that MOVES or HEATS the printer first reads print_stats.state and refuses
unless the printer is idle (standby/complete/cancelled) -- never assume "paused".
CANCEL is never sent (Creality's END_PRINT does M84: position lost).

Workflow (part still on the bed, motors may be off):
  state                         what is the printer doing
  fetch FILE                    download the failed job's G-code (name from `state`/history)
  layers FILE                   z / time / feature table -> where did it fail
  outline FILE --z Z            outer-wall bbox at height Z -> where to probe the part
  home-xy                       G28 X Y only (Creality G28 Z touches down at bed CENTRE)
  assume-z Z                    declare the current nozzle height (after home-xy, Z is
                                only approximate; set it a bit HIGH, the probe finds the bed)
  probe X,Y [X,Y ...] -o F      touch-probe each point (travel at --safe-z), save JSON
  analyse F --bed-margin M      bed points = probes outside the part bbox; fits the frame
                                offset to the stored mesh, reports part tops in print coords
                                and the restart layer + first-layer flow
  set-frame OFFSET              convert to the original print's frame + load mesh `default`
  build FILE --from-z L [--flow1 125] [--home none|center]
  upload GCODE / start GCODE    gated start

--home center is only safe when the bed centre (150,150) is EMPTY (e.g. a ring); otherwise use
the probe frame (home-xy, assume-z, probe, set-frame, build --home none).
"""
import argparse, json, math, re, sys, time, urllib.request

import os
HOST = os.environ.get("PRINTRESCUE_HOST", "")        # e.g. http://192.168.1.50:7125
IDLE = ("standby", "complete", "cancelled")


# ---- moonraker ---------------------------------------------------------------
def q(path):
    if not HOST:
        sys.exit("Set PRINTRESCUE_HOST=http://<printer>:7125 (Moonraker) or pass --host.")
    return json.load(urllib.request.urlopen(HOST + path, timeout=15))["result"]


def status(*objs):
    return q("/printer/objects/query?" + "&".join(objs))["status"]


def gcode(script, timeout=900):
    r = urllib.request.Request(HOST + "/printer/gcode/script", data=json.dumps({"script": script}).encode(),
                               headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(r, timeout=timeout).read().decode()[:200]


def require_idle(homed=False):
    s = status("print_stats=state", "toolhead=homed_axes,position")
    st, h = s["print_stats"]["state"], s["toolhead"]["homed_axes"]
    if st not in IDLE:
        sys.exit("REFUSED: printer is '%s' (not idle). Nothing sent." % st)
    if homed and h != "xyz":
        sys.exit("REFUSED: axes not homed ('%s'). Run home-xy + assume-z first." % h)
    return s["toolhead"]["position"]


# ---- G-code analysis -----------------------------------------------------------
def layers(path):
    rows, cur = [], None
    for line in open(path):
        if line.startswith(";Z:"):
            if cur: rows.append(cur)
            cur = [float(line[3:]), None, set()]
        elif cur and line.startswith(";TIME_ELAPSED:") and cur[1] is None:
            cur[1] = float(line.split(":")[1])
        elif cur and line.startswith(";TYPE:"):
            cur[2].add(line[6:].strip())
    if cur: rows.append(cur)
    return rows


def outline(path, z):
    xs, ys, zc, typ, px, py = [], [], None, None, None, None
    for line in open(path):
        if line.startswith(";Z:"): zc = float(line[3:]); continue
        if line.startswith(";TYPE:"): typ = line[6:].strip(); continue
        if not line.startswith("G1"): continue
        mx, my = re.search(r" X([-\d.]+)", line), re.search(r" Y([-\d.]+)", line)
        nx = float(mx.group(1)) if mx else px; ny = float(my.group(1)) if my else py
        if zc is not None and abs(zc - z) < 1e-6 and typ == "Outer wall" and " E" in line and px is not None:
            xs += [px, nx]; ys += [py, ny]
        px, py = nx, ny
    return min(xs), max(xs), min(ys), max(ys)


def mesh_default(profile="default"):
    s = status("bed_mesh=profiles")["bed_mesh"]["profiles"][profile]
    p, M = s["mesh_params"], s["points"]
    xs = [p["min_x"] + i*(p["max_x"] - p["min_x"])/(p["x_count"] - 1) for i in range(p["x_count"])]
    ys = [p["min_y"] + j*(p["max_y"] - p["min_y"])/(p["y_count"] - 1) for j in range(p["y_count"])]

    def mz(x, y):                                       # bilinear, clamped (printer: bicubic)
        x = min(max(x, xs[0]), xs[-1]); y = min(max(y, ys[0]), ys[-1])
        i = max(0, min(len(xs) - 2, max(k for k in range(len(xs)) if xs[k] <= x)))
        j = max(0, min(len(ys) - 2, max(k for k in range(len(ys)) if ys[k] <= y)))
        u = (x - xs[i])/(xs[i + 1] - xs[i]); v = (y - ys[j])/(ys[j + 1] - ys[j])
        return (M[j][i]*(1 - u)*(1 - v) + M[j][i + 1]*u*(1 - v) + M[j + 1][i]*(1 - u)*v + M[j + 1][i + 1]*u*v)
    return mz


def build(path, L, flow1, home, purge_x, out, pa=None):
    src = open(path).read()
    define = [l for l in src.splitlines() if l.startswith("EXCLUDE_OBJECT_DEFINE")]
    i = src.index(";LAYER_CHANGE\n;Z:%g\n" % L); j = src.index(";AFTER_LAYER_CHANGE", i)
    k = src.index(";LAYER_CHANGE\n", j)
    temps = re.search(r"START_PRINT EXTRUDER_TEMP=(\d+) BED_TEMP=(\d+)", src)
    te, tb = (temps.group(1), temps.group(2)) if temps else ("220", "60")
    fans = re.findall(r"^(M106 (?:P\d+ )?S\d+)$", src[:j], re.M)
    fan_state = {}
    for f in fans: fan_state[f.split(" S")[0]] = f              # last setting per fan before L
    homing = ("M104 S140\nG28\nBED_MESH_PROFILE LOAD=default\n" if home == "center"
              else "; no homing: frame set by `printrescue set-frame` before start\n")
    head = (f"; printrescue: restart of {path} from layer z {L} (home={home}, first-layer flow {flow1}%)\n"
            + "\n".join(define) + f"\nM140 S{tb}\nM190 S{tb}\n" + homing
            + f"G90\nM83\nG1 Z15 F600\nG1 X{purge_x} Y40 F9000\nM109 S{te}\nG1 Z0.3 F600\nG92 E0\n"
            f"G1 Y200 E10 F1500\nG1 E-0.6 F2400\nG1 Z15 F600\n" + "\n".join(fan_state.values())
            + ("\nSET_PRESSURE_ADVANCE ADVANCE=%g" % pa if pa is not None else "")
            + f"\nM221 S{flow1}\nG92 E0\n")
    open(out, "w").write(head + src[j:k] + "M221 S100\n" + src[k:])
    return out


# ---- CLI --------------------------------------------------------------------------
def main():
    global HOST
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", help="Moonraker URL (or env PRINTRESCUE_HOST)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("state")
    a = sp.add_parser("fetch"); a.add_argument("file"); a.add_argument("-o")
    a = sp.add_parser("layers"); a.add_argument("gcode"); a.add_argument("--max", type=int, default=60)
    a = sp.add_parser("outline"); a.add_argument("gcode"); a.add_argument("--z", type=float, required=True)
    sp.add_parser("home-xy")
    a = sp.add_parser("assume-z"); a.add_argument("z", type=float)
    a = sp.add_parser("probe"); a.add_argument("pts", nargs="+"); a.add_argument("-o", required=True)
    a.add_argument("--safe-z", type=float, default=17.0)
    a = sp.add_parser("analyse"); a.add_argument("probes"); a.add_argument("--gcode", required=True)
    a.add_argument("--z", type=float, required=True, help="a layer height the part reached (for the outline)")
    a = sp.add_parser("set-frame"); a.add_argument("offset", type=float)
    a = sp.add_parser("build"); a.add_argument("gcode"); a.add_argument("--from-z", type=float, required=True)
    a.add_argument("--flow1", type=int, default=100); a.add_argument("--home", choices=("none", "center"), default="none")
    a.add_argument("--purge-x", type=float, default=292.0, help="purge line x, outside the part")
    a.add_argument("--pa", type=float, help="SET_PRESSURE_ADVANCE value, if your start macro sets one"); a.add_argument("-o")
    a = sp.add_parser("upload"); a.add_argument("gcode")
    a = sp.add_parser("start"); a.add_argument("gcode")
    A = ap.parse_args()
    if A.host: HOST = A.host.rstrip("/")

    if A.cmd == "state":
        s = status("print_stats", "toolhead=homed_axes,position", "extruder=temperature,target",
                   "heater_bed=temperature,target", "bed_mesh=profile_name", "virtual_sdcard=progress",
                   "filament_switch_sensor%20filament_sensor")
        p = s["print_stats"]
        print("state %s | %s | %.1f min | %.1f m" % (p["state"], p["filename"], p["print_duration"]/60, p["filament_used"]/1000))
        print("homed '%s' pos %s | nozzle %.0f/%.0f bed %.0f/%.0f | mesh '%s' | progress %.3f | filament %s"
              % (s["toolhead"]["homed_axes"], [round(v, 2) for v in s["toolhead"]["position"][:3]],
                 s["extruder"]["temperature"], s["extruder"]["target"], s["heater_bed"]["temperature"],
                 s["heater_bed"]["target"], s["bed_mesh"]["profile_name"], s["virtual_sdcard"]["progress"],
                 s["filament_switch_sensor filament_sensor"]["filament_detected"]))
        for j in q("/server/history/list?limit=3")["jobs"]:
            print("  history:", j["filename"], j["status"], time.strftime("%m-%d %H:%M", time.localtime(j["start_time"])))
    elif A.cmd == "fetch":
        out = A.o or A.file
        urllib.request.urlretrieve(HOST + "/server/files/gcodes/" + urllib.request.quote(A.file), out); print(out)
    elif A.cmd == "layers":
        rows = layers(A.gcode); tot = rows[-1][1] or 1
        for z, t, ty in rows[:A.max]:
            print("z %6.2f  t %5.2f h (%5.1f%%)  %s" % (z, (t or 0)/3600, (t or 0)/tot*100, ", ".join(sorted(ty - {"Custom"}))))
        print("... %d layers, %.1f h" % (len(rows), tot/3600))
    elif A.cmd == "outline":
        print("outer wall at z %.2f: x %.2f..%.2f  y %.2f..%.2f" % ((A.z,) + outline(A.gcode, A.z)))
    elif A.cmd == "home-xy":
        require_idle(); print(gcode("G28 X Y", 300)); print(status("toolhead=homed_axes,position")["toolhead"])
    elif A.cmd == "assume-z":
        require_idle(homed=True); print(gcode("SET_KINEMATIC_POSITION Z=%g" % A.z))
    elif A.cmd == "probe":
        require_idle(homed=True)
        pts = [tuple(map(float, p.split(","))) for p in A.pts]
        lines = ["G90", "G1 Z%g F900" % A.safe_z]
        for x, y in pts:
            lines += ["G1 Z%g F900" % A.safe_z, "G1 X%g Y%g F6000" % (x, y), "PROBE", "G1 Z%g F900" % A.safe_z]
        t0 = time.time(); print(gcode("\n".join(lines + ["M400"])))
        res = {}
        for m in q("/server/gcode_store?count=500")["gcode_store"]:
            if m["time"] >= t0 - 1 and "probe at " in m["message"]:
                xy, z = m["message"].split("probe at ")[1].split(" is z=")
                res[xy] = float(z)
        json.dump(res, open(A.o, "w"), indent=1); print(json.dumps(res, indent=1))
    elif A.cmd == "analyse":
        P = {tuple(map(float, k.split(","))): v for k, v in json.load(open(A.probes)).items()}
        x0, x1, y0, y1 = outline(A.gcode, A.z); mz = mesh_default()
        inside = lambda x, y: x0 - 0.5 <= x <= x1 + 0.5 and y0 - 0.5 <= y <= y1 + 0.5
        bed = {k: v for k, v in P.items() if not inside(*k)}; part = {k: v for k, v in P.items() if inside(*k)}
        offs = sorted(v - mz(*k) for k, v in bed.items())
        o = offs[len(offs)//2] if len(offs) % 2 else (offs[len(offs)//2 - 1] + offs[len(offs)//2])/2
        sd = (sum((x - sum(offs)/len(offs))**2 for x in offs)/len(offs))**0.5
        print("frame offset (probe - mesh) median %.3f, sd %.3f over %d bed points" % (o, sd, len(offs)))
        tops = {k: v - o - mz(*k) for k, v in part.items()}
        for k, t in sorted(tops.items()): print("  part %s top %.2f (print coords)" % (k, t))
        zs = sorted(r[0] for r in layers(A.gcode)); hi, lo = max(tops.values()), min(tops.values())
        L = next(z for z in zs if z >= hi + 0.05)
        gap = L - lo; flow = 100 + min(30, max(0, int(round((gap - 0.2)*100/5))*5))   # +5 % per 0.05 mm of extra gap
        print("restart layer %.2f (clears top %.2f by %.2f); worst gap %.2f -> first-layer flow ~%d%%" % (L, hi, L - hi, gap, flow))
        print("next: set-frame %.3f ; build %s --from-z %g --flow1 %d" % (o, A.gcode, L, flow))
    elif A.cmd == "set-frame":
        pos = require_idle(homed=True)
        print(gcode("SET_KINEMATIC_POSITION Z=%.4f\nBED_MESH_PROFILE LOAD=default" % (pos[2] - A.offset)))
        print(status("toolhead=position", "bed_mesh=profile_name"))
    elif A.cmd == "build":
        out = A.o or "restart_z%g_%s" % (A.from_z, A.gcode.split("/")[-1])
        print(build(A.gcode, A.from_z, A.flow1, A.home, A.purge_x, out, A.pa))
    elif A.cmd == "upload":
        import subprocess
        print(subprocess.run(["curl", "-s", "-F", "file=@" + A.gcode, HOST + "/server/files/upload"],
                             capture_output=True, text=True).stdout)
    elif A.cmd == "start":
        require_idle()
        r = urllib.request.Request(HOST + "/printer/print/start?filename=" + urllib.request.quote(A.gcode.split("/")[-1]),
                                   data=b"", method="POST")
        print(urllib.request.urlopen(r, timeout=30).read().decode())


if __name__ == "__main__":
    main()
