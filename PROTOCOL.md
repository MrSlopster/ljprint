# LuckJingle Thermal Printer — Bluetooth Protocol Reference

Reverse-engineered from `com.dingdang.newprint` (Luck Jingle) v2.7.16.
Source: `Luck+Jingle_2.7.16_APKPure.xapk` → `com.dingdang.newprint.apk`,
package `com.luckprinter.sdk_new.*`.

The printer exposes two Bluetooth transport options; the **BLE GATT** path is
what current firmware uses, classic **SPP** is kept for older units.

---

## 1. Transport: Bluetooth Low Energy (GATT)

### 1.1 Service and characteristics

| Role                     | UUID (base `xxxx-0000-1000-8000-00805f9b34fb`) | Properties        |
|--------------------------|-----------------------------------------------|-------------------|
| Printer service          | `0000ff00-0000-1000-8000-00805f9b34fb`         | —                 |
| Write (commands / data)  | `0000ff02-0000-1000-8000-00805f9b34fb`         | Write, Write No Response |
| Response / async events  | `0000ff01-0000-1000-8000-00805f9b34fb`         | Notify            |
| Credit / MTU signalling  | `0000ff03-0000-1000-8000-00805f9b34fb`         | Notify            |

Notes:

* **`ff02`** is the only write channel (client → printer).
* **`ff01`** is the printer→client response channel (status replies, `"OK"`,
  async events such as paper-out). Subscribe before issuing queries.
* **`ff03`** is the flow-control channel. Subscribe to it before sending any
  payload.
* Verified against real hardware (advertised name `DP_D1_BC3B`, MAC
  `60:6E:41:53:BC:3B`) on 2026-07-18. The decompiled SDK
  (`M3/a.java` → `onConnectSuccess`) assigns the write role to whichever
  characteristic advertises `PROPERTY_WRITE_NO_RESPONSE`; on production
  firmware that is `ff02`. The minor-UUID suffix may differ across clones,
  so always match by `startsWith("0000ff00")` and inspect properties.

### 1.2 Connection sequence

```
1. Scan / connect to MAC (advertisement name typically "LuckJingle-..." or
   similar; the SDK matches by service UUID, not name).
2. Discover services, locate service 0000ff00 and the three characteristics.
3. Subscribe to NOTIFY on ff01  (response channel).
4. Subscribe to NOTIFY on ff03  (credit/MTU channel).
   - First notification on ff03 is always [0x02, mtuLo, mtuHi] (3 bytes):
     the printer's preferred ATT_MTU. The client should then request
     ATT_MTU = 3 + (mtuHi<<8 | mtuLo) so the write payload per packet is
     exactly `mtu` bytes.
   - Subsequent ff03 notifications are 2-byte credits (see §1.3).
```

The packet size used for chunking writes is therefore negotiated per session
(default 20 bytes before MTU negotiation).

### 1.3 Credit-based flow control

The printer pushes 2-byte notifications on `ff03` of the form
`[0x01, n]` meaning "you may send **n more** packets." The client MUST
maintain a credit counter:

```
on ff03 notify [0x01, n]:  credit += n
on ff03 notify [0x02, lo, hi]:  request ATT_MTU = 3 + (lo | hi<<8)
                                 (then ignore as a credit)
```

Send loop (per write call):

```
while bytes remain:
    if credit == 0:  wait (up to 30 s) for the next [0x01, n] notification
    packets_this_round = min(credit - (1 if credit >= 3 else 0),
                             ceil(remaining / packet_size))
    for each packet:  write ff02 (write-without-response);  credit -= 1
```

The `credit - 1` reservation when `credit >= 3` keeps one packet "in flight"
so the printer can emit the next `[0x01, n]` before the client stalls.

### 1.4 Responses on ff01

Printer → client notifications on `ff01`:

| Payload                                 | Meaning                                              |
|-----------------------------------------|------------------------------------------------------|
| ASCII `"OK"` (`4F 4B`)                  | Generic command acknowledgement                      |
| `AA …`                                  | Print-job complete marker                            |
| 1 status byte (see §3.1)                | Reply to status query                                |
| ASCII string + `\|`-separated fields    | Reply to `printerInfoLuck` (`name\|...\|SN\|battery`) |
| `FC FF 00 02 45 FE 01 BB`               | Async label-paper error event                        |
| other byte sequences                    | Routed to `getUploadErrorCode()` (device-specific)   |

---

## 2. Transport: Classic Bluetooth (SPP, legacy)

* RFCOMM UUID: `00001101-0000-1000-8000-00805F9B34FB` (standard SPP).
* Open a `BluetoothSocket`, write the same command stream to its
  `DataOutputStream`. The SDK chunks writes at **16 KiB**; reads come back on
  the same socket's `DataInputStream`.
* No credit/MTU dance — classic RFCOMM provides its own back-pressure.

The command set (§3) is identical on both transports.

---

## 3. Command language

ESC/POS-compatible with a vendor command group prefixed `10 FF`
(`DLE FF`) for printer-control extensions. All multi-byte integers are
**little-endian** unless noted.

Constants used below: `ESC=1B` `DLE=10` `GS=1D` `US=1F` `FF=0C` `NAK=15`
`DC2=12` `~ = FF` (Java signed-byte for `0xFF`).

### 3.1 Status / device queries

| Command (hex)            | Reply                              | Notes                                |
|--------------------------|------------------------------------|--------------------------------------|
| `10 FF 40`               | 1 status byte (bitfield, below)    | Real-time printer status             |
| `10 FF 50 F1`            | `[tag, battery%]`                  | Battery (0–100)                      |
| `10 FF 11`               | 1 byte, density                    | Get print density                    |
| `10 FF 13`               | 1–2 bytes, shutdown minutes        | Get auto-shutdown time               |
| `10 FF 20 EF`            | ASCII string                       | Device boot version                  |
| `10 FF 20 F0`            | ASCII model name                   | e.g. `LJ-…`                          |
| `10 FF 20 F1`            | ASCII firmware version             |                                      |
| `10 FF 20 F2`            | ASCII serial number                |                                      |
| `10 FF 20 A0`            | 1 byte, speed                      | Get print speed                      |
| `10 FF B0`               | 1 byte, time-format flag           | 12h/24h                              |
| `10 FF 70`               | `\|`-separated info blob           | Deprecated combined info             |

Status byte bitfield (reply to `10 FF 40`):

| Bit | Meaning when set (1)                  |
|-----|---------------------------------------|
| 0   | Printing                              |
| 1   | Powered on / open                     |
| 2   | Out of paper                          |
| 3   | Low battery                           |
| 4   | Overheat (combined with bit 6)        |
| 5   | Needs recharge                        |
| 6   | Overheat (combined with bit 4)        |

### 3.2 Printer control (vendor `10 FF` group)

| Command (hex)                  | Effect                                   |
|--------------------------------|------------------------------------------|
| `10 FF F1 <mode>`              | Enable printer (mode=3 typical)          |
| `00 00 00 00 00 00 00 00 00 00 00 00` | 12-byte wake-up pulse (12 × `0x00`) |
| `10 FF F1 45`                  | Stop / flush current print job           |
| `10 FF 04`                     | Reset / recovery                         |
| `10 FF 10 00 <density>`        | Set density (vendor-scaled)              |
| `10 FF 15 <wL> <wH>`           | Set print width in dots                  |
| `10 FF C0 <speed>`             | Set print speed                          |
| `10 FF 12 <minL> <minH>`       | Set auto-shutdown timeout (minutes)      |
| `10 FF 30 27 <mode>`           | Set printer mode                         |
| `1F 70 01 <level>`             | Set heating level                        |
| `1F 80 <kind> <mask>`          | Set paper type (e.g. `1F 80 01 40` tattoo) |
| `1F 11 <n>`                    | Auto adjust paper position               |
| `1F 11 11 <n>`                 | Reverse feed n dots                      |
| `FC FF 00 02 45 02 00 46`      | Set platform identifier (sent at startup)|
| `10 FF 53 4A <flag> <YYYYH> <YYYYL> <MM> <DD> <hh> <mm> <ss>` | Set RTC |

### 3.3 Print data — ESC/POS raster bit image

The standard ESC/POS raster command is used for all image/bitmap data.
There is **no native text-print command** in the firmware; the app always
rasterises text via the OS and sends it through this command.

#### 3.3.1 Uncompressed (1-bit)

```
1D 76 30 <mode> <xL> <xH> <yL> <yH> <pixel data>
```

* `mode`: `00` normal (mode 0; modes 1–3 = double-width / double-height /
  both, but the SDK always uses `00`).
* `x = ceil(image_width_px / 8)` — bytes per row.
* `y = image_height_px`.
* `xL xH` and `yL yH` are little-endian.
* Pixel data is row-major, MSB-first within each byte, **1 = black**.
  Each row is padded with zero bits to the next byte boundary.

Typical print widths: **384 px** (58 mm "normal" printer), 832 px (A4).

#### 3.3.2 Run-length / vendor-compressed (optional)

Two compressed variants exist in `libPrinterNative.so`:

```
GS FE  <xH> <xL> <yH> <yL> <len:4 BE> <payload>   # "Lihu" compression
US 10  <xH> <xL> <yH> <yL> <len:4 BE> <payload>   # "ESC"  compression
```

The payload is produced by `Compress.codeLihu()` / `Compress.codeESC()` in
`com.print.libnative.Compress`. The uncompressed `GS v 0` form (3.3.1) is
always accepted and is what the demo uses.

#### 3.3.3 Grayscale (4-level) — vendor extension

```
GS <0xFF new | 0x47 old> 59 <levels> <xL> <xH> <yL> <yH> <pixel data>
```

Used when the printer is in grayscale mode; pixel data is packed
2 pixels per byte (4 bits each). For firmware that supports it the SDK
applies Floyd–Steinberg dithering. Not required for plain text.

### 3.4 Print motion (ESC/POS standard)

| Command (hex)        | Effect                                        |
|----------------------|-----------------------------------------------|
| `1B 4A <n>`          | Feed paper `n` dots (1–255) after printing    |
| `1D 0C`              | Form feed / position (page mode)              |

### 3.5 Recommended print job sequence (text/image)

```
1. 10 FF F1 03                       # enable printer
2. 00 × 12                           # wake-up
3. 1D 76 30 00 xL xH yL yH <data>    # GS v 0 raster image
4. 1B 4A <end_line_dots>             # ESC J n  — feed trailing paper
                                     #   end_line_dots = 80 for 384-px heads
                                     #   end_line_dots = 120 otherwise
5. 10 FF F1 45                       # stop / flush
   -> wait for "OK" or 0xAA on ff01
```

---

## 4. Discovery hints

* Verified hardware (`DP_D1_BC3B`, model `D1Y-KD`) does **not** advertise the
  `0000ff00-...` service in its advertisement packets — only `0000fee7`,
  `000018f0`, and a custom UUID appear there. The `ff00` service must be
  discovered via GATT after connecting. Reliable discovery is by **friendly
  name prefix**: `LuckJingle`, `LJ-*`, `DP_*`, `GT-*`, `AiYin-*`, etc.,
  typically with the last 2 hex octets of the MAC appended.
* Friendly name typically matches `LuckJingle`, `LJ-*`, `GT-*`,
  `AiYin-*`, etc. depending on device variant (see `PrinterType`:
  `normal`, `a4`, `sheet_label`).
* Older units expose SPP only — pair via `00001101-...` and use a
  classic RFCOMM socket.

---

## 5. Source cross-references (decompiled APK)

| Concern                         | Path in APK (`sources/`)                                            |
|---------------------------------|---------------------------------------------------------------------|
| GATT service/char setup         | `M3/a.java` (`onConnectSuccess`)                                    |
| BLE write loop + credit logic   | `M3/f.java` (`l()`)                                                 |
| MTU + credit notify parsing     | `M3/d.java` (`onCharacteristicChanged`)                             |
| Response channel parsing        | `M3/c.java` (`onCharacteristicChanged`)                             |
| SPP classic transport           | `N3/c.java`                                                         |
| Print job sequencing            | `com/luckprinter/sdk_new/device/normal/base/BaseNormalDevice.java`  |
| Raster encoder (`GS v 0`)       | `com/luckprinter/sdk_new/device/normal/base/PrinterImageProcessor.java` |
| Compression wrappers            | `com/print/libnative/Compress.java` + `libPrinterNative.so`         |
| Command-type enum               | `com/luckprinter/sdk_new/device/custom/CmdType.java`                |

All findings above were derived from these files; byte values that appear
negative in the Java source (e.g. `-1`, `-15`) are the corresponding
unsigned bytes (`0xFF`, `0xF1`).
