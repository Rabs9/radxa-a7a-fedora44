# Booting the backported kernel on the Fedora board

**Everything is staged. Nothing about the next boot has changed.**

The board still boots `g_1008_800` (the DDK, GPU at 1008 MHz) exactly as before.
The new kernel sits beside it as entry 2 and is only used if someone selects it.

## What was installed

| | |
|---|---|
| `/boot/vmlinuz-6.6.98-pvr+` | the backported kernel, 40,159,240 bytes |
| `/usr/lib/linux-image-6.6.98-pvr+/sun60i-a733-cubie-a7a.dtb` | DTB with `img,img-rogue` + `core`/`sys`/`mem` clocks |
| `/lib/modules/6.6.98-pvr+/` | 1,303 modules, its own complete tree |
| `/lib/firmware/powervr/rogue_36.56.104.183_v1.fw` | the DDK blob, copied under the name the driver builds at runtime |
| `/etc/modprobe.d/a7a-pvr-manual.conf` | **blacklists `powervr`** |
| extlinux entry `fedora-6.6.98-pvr+` | position 2, **not default** |

Nothing existing was modified. No kernel, module tree, DTB or boot entry was
overwritten.

### The firmware copy

```
43eb5d054ee98bc9...  /lib/firmware/rgx.fw.36.56.104.183
43eb5d054ee98bc9...  /lib/firmware/powervr/rogue_36.56.104.183_v1.fw
```

Same file, two names. The driver builds `powervr/rogue_<B>.<V>.<N>.<C>_v1.fw`
from the BVNC it reads out of the hardware, so this is the name it will ask for.

### Why powervr is blacklisted

Its modalias matches the new DTB's `img,img-rogue`, so udev would load an
entirely untested GPU driver during early boot. Blacklisted, the new kernel is
just the stock BSP kernel minus nouveau - the GPU driver only appears when
loaded by hand, over ssh, with `dmesg -w` running.

That also means the DDK is *not* available on this boot: `pvrsrvkm` is not in
the `6.6.98-pvr+` module tree. The desktop will be software-rendered until
`powervr` is loaded and Mesa picks it up. ssh is unaffected.

## To try it

```
sudo a7a-extlinux default fedora-6.6.98-pvr+
sudo reboot
```

Then, once it is up:

```
dmesg -w &                     # or a second ssh session
sudo modprobe -r powervr       # no-op, just proving the blacklist
sudo rm /etc/modprobe.d/a7a-pvr-manual.conf   # or use --allow-unsupported
sudo modprobe powervr
```

What to look for, in order:

1. `powervr` binds the node at all - anything from the driver in `dmesg`
2. `loaded firmware powervr/rogue_36.56.104.183_v1.fw`
3. absence of `Unsupported features / quirks / enhancements in firmware image`
   - that is the driver comparing the firmware's declared device info against
     the hardware, and is the single most likely failure
4. a `card` appearing whose driver name is `powervr`
5. `dmesg | grep -i 'fw_bvnc\|hw_bvnc\|unsupported'`

## To undo

From a booted system:

```
sudo a7a-extlinux fallback
sudo reboot
```

If it does **not** boot: the board has a serial console on `ttyAS0` at 115200,
and extlinux has `timeout 50` (5 seconds) with `prompt 0`. Press a key on the
serial line during those five seconds to get the menu and pick
`g_1008_800` (or `fedora-recovery`). Without serial, the fallback is pulling
the card.

Boot risk is genuinely low - the new Image is the BSP kernel with nouveau
removed and two extra *modules* - but it has never been booted, so the serial
line is the thing to have ready.

## The real unknowns

Not whether it boots. Whether it *binds*:

- **Reset.** Added to `pvr_probe()` because the DDK deasserts `reset_bus` from
  its own power-on path and the in-tree driver has no reset handling at all.
  Untested.
- **Clocks.** `core`/`sys`/`mem` are duplicate phandles appended to the existing
  list so the DDK's by-name lookups still work. If the CCU dislikes a clock
  being claimed twice, this is where it shows.
- **Firmware acceptance.** The blob is structurally identical to the published
  ones, but the driver validates the firmware's device info against the
  hardware. If 36.56 declares a feature the v6.8 driver does not know, it
  refuses - and that is a legitimate outcome, not a mistake.
- **Power island.** The img-bxm DDK does not touch the PPU registers (the older
  img-rgx variant does), so reset + clocks should be the whole sequence. If
  register reads come back as zeroes or the board hangs, that assumption was
  wrong.
