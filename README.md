# klipper-print-rescue

Continue a failed 3D print **from the layer where it stopped**, with the part still on the
bed, on a Klipper + Moonraker printer. It works even after the job has ended and the
motors have switched off, and even when re-homing would crash into the part.

It uses the printer's own probe as a height gauge on top of the printed part. Probing the
bed beside the part as well gives the part's height and how far the printer's current
coordinates are from the original print's. You then continue from the right layer with
the original bed mesh, without homing on the part.

Tested on a Creality **Ender-3 V3 Plus** (Creality's Klipper fork, load-cell bed probe):
a 25 × 25 cm plate whose spool jammed 2.8 mm in was finished with no visible seam.

## When to use it

| Situation | Method |
|---|---|
| Print **still paused**, never cancelled | Rewind in place: `M26 S<byte offset of the layer>` while paused, then `RESUME` |
| Job ended/cancelled, **bed centre empty** (e.g. a ring) | `build --home center` from the failed layer |
| Job ended, **part covers the bed centre** | Probe-frame procedure below |

## Probe-frame procedure

```bash
export PRINTRESCUE_HOST=http://<printer-ip>:7125

printrescue.py state                          # idle? which job failed?
printrescue.py fetch <job.gcode> -o job.gcode
printrescue.py layers job.gcode               # height / time / features per layer
printrescue.py outline job.gcode --z 2.0      # part outline at a height it reached

printrescue.py home-xy                        # X/Y only, never Z on top of the part
printrescue.py assume-z 12                    # declare the nozzle height a little HIGH
# heat the bed to the print temperature and let it soak ~10 min (the mesh was made hot)

# bed points ~15 mm outside the part, part points ~1 mm inside its outer wall
printrescue.py probe 10,10 24.6,26.1 290,10 275.4,26.1 10,290 24.6,273.9 290,290 275.4,273.9 -o probes.json

printrescue.py analyse probes.json --gcode job.gcode --z 2.0
#   frame offset (probe - mesh) median 2.360, sd 0.103 over 8 bed points
#   part tops 2.56 .. 2.94 (print coords)
#   restart layer 3.00; worst gap 0.44 -> first-layer flow ~125%

printrescue.py set-frame 2.360                # switch to the original frame + mesh
printrescue.py build job.gcode --from-z 3.0 --flow1 125 -o restart.gcode
printrescue.py upload restart.gcode
printrescue.py start restart.gcode            # be at the printer for the first layer
```

`examples/` has the probe readings from the rescue above.

## Safety

- Every command that **moves or heats** the printer reads `print_stats.state` first and
  refuses unless the printer is idle (`standby` / `complete` / `cancelled`). People act
  at the printer between your commands, so never assume "paused".
- It never sends `CANCEL_PRINT`. On many printers the end macro turns the motors off
  (`M84`), which loses the position you need for an in-place rewind.
- The restart starts **one layer above the highest probed point**. A higher first-layer
  flow fills low spots, so the nozzle never ploughs into the part. Shave seam blobs at the
  corners first.
- `build --home center` runs the printer's own `G28`. Use it only if the part leaves the
  bed centre empty; many printers touch down there to home Z.

## Notes for Creality Klipper (Ender-3 V3 / K1 family)

- `G28 X Y` also homes Z sensorlessly at the top and drops to about 10 mm, so Z is only
  approximate afterwards. `assume-z`, then `PROBE`, sorts it out.
- `PAUSE` drops the nozzle to 140 °C and the extruder current to its minimum; `RESUME`
  restores them.
- The `default` mesh profile persists for the session; `set-frame` loads it.
- Pass `--pa <value>` to `build` if your start macro sets pressure advance.

## Requirements

Python 3.8+, standard library only. Moonraker reachable over HTTP.

## Licence

MIT
