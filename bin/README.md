# Miner binary goes here

This kit does **not** ship the `primo-arm-miner` binary — it's third-party,
GPL-3.0 software from [PrimoLab/primo-arm-miner](https://github.com/PrimoLab/primo-arm-miner).

`setup.sh` looks for the binary in this order:
1. `PRIMO_BIN=/path/to/primo-arm-miner` if you set it,
2. `bin/primo-arm-miner` (drop it here — this is the easy path),
3. an already-installed `/usr/local/bin/primo-arm-miner` on the target.

To get an ARMv8 binary: download a release from the upstream repo, or copy it
off an existing miner, e.g.:

```bash
scp user@existing-miner:/usr/local/bin/primo-arm-miner bin/primo-arm-miner
```

One binary (`-march=armv8-a+crypto`) runs on any ARMv8 board (Orange Pi
Zero2W/Zero3 = Allwinner H618, RK3588, Raspberry Pi 4/5, etc.).
