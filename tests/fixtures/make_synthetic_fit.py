"""Generate a tiny SYNTHETIC FIT activity for tests — no personal GPS data.

The real `sample_activity.fit` is a private run (gitignored). CI therefore
skipped the cadence regression test. This script emits a minimal but valid FIT
file (file_id + records + laps + session, NO position data) that exercises the
same `parse_fit_file` path, so the cadence guard runs everywhere.

Run:  python tests/fixtures/make_synthetic_fit.py
Out:  tests/fixtures/synthetic_activity.fit   (committed)

Hand-rolled encoder — no third-party FIT writer needed. Only the message fields
that `parse_fit_file` reads are emitted. Regenerate + verify with the assertions
in tests/test_main.py::test_parse_fit_summary_and_cadence.
"""
import struct
from datetime import datetime, timezone
from pathlib import Path

# ── FIT CRC-16 (standard FIT algorithm) ───────────────────────────────────────
_CRC_TABLE = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data: bytes) -> int:
    crc = 0
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc & 0xFFFF


# ── Base types ────────────────────────────────────────────────────────────────
ENUM, UINT8, UINT16, UINT32 = 0x00, 0x02, 0x84, 0x86
SIZE = {ENUM: 1, UINT8: 1, UINT16: 2, UINT32: 4}
PACK = {ENUM: "B", UINT8: "B", UINT16: "<H", UINT32: "<I"}

# FIT epoch = 1989-12-31 00:00:00 UTC
FIT_EPOCH = 631065600


def to_fit_time(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) - FIT_EPOCH


class FitBuilder:
    def __init__(self):
        self.data = bytearray()

    def define(self, local_type, global_msg, fields):
        # fields: list of (field_def_num, base_type)
        rec = bytearray([0x40 | local_type, 0x00, 0x00])
        rec += struct.pack("<H", global_msg)
        rec.append(len(fields))
        for fdn, base in fields:
            rec += bytes([fdn, SIZE[base], base])
        self.data += rec

    def data_msg(self, local_type, fields, values):
        rec = bytearray([local_type])
        for (fdn, base), val in zip(fields, values):
            rec += struct.pack(PACK[base], val)
        self.data += rec

    def build(self) -> bytes:
        body = bytes(self.data)
        header = bytearray()
        header.append(14)            # header size
        header.append(0x20)          # protocol 2.0
        header += struct.pack("<H", 2100)        # profile version
        header += struct.pack("<I", len(body))   # data size
        header += b".FIT"
        header += struct.pack("<H", fit_crc(bytes(header[:12])))  # header CRC
        full = bytes(header) + body
        return full + struct.pack("<H", fit_crc(full))            # file CRC


# ── Message field layouts (only what parse_fit_file reads) ────────────────────
F_FILE_ID = [(0, ENUM), (1, UINT16), (4, UINT32)]                 # type, manuf, time_created
F_RECORD  = [(253, UINT32), (3, UINT8), (4, UINT8), (5, UINT32),  # ts, hr, cad, dist
             (6, UINT16), (2, UINT16)]                            # speed, altitude
F_LAP     = [(253, UINT32), (7, UINT32), (9, UINT32), (15, UINT8),  # ts, elapsed, dist, avg_hr
             (16, UINT8), (17, UINT8), (21, UINT16)]                # max_hr, avg_cad, ascent
F_SESSION = [(253, UINT32), (2, UINT32), (7, UINT32), (9, UINT32),  # ts, start, elapsed, dist
             (11, UINT16), (16, UINT8), (17, UINT8), (18, UINT8),   # cal, avg_hr, max_hr, avg_cad
             (22, UINT16), (23, UINT16)]                            # ascent, descent


def main():
    start = datetime(2026, 5, 1, 6, 0, 0)
    t0 = to_fit_time(start)

    fb = FitBuilder()

    # file_id (activity)
    fb.define(0, 0, F_FILE_ID)
    fb.data_msg(0, F_FILE_ID, [4, 255, t0])

    # records: 18 points @ 10s, hr 120→170, speed ~3.3 m/s, altitude 60→75 m
    fb.define(1, 20, F_RECORD)
    n = 18
    for i in range(n):
        ts = t0 + i * 10
        hr = 120 + i * 3
        cad = 86
        dist_m = i * 33                       # ~3.3 m/s
        alt_m = 60 + i                        # rising
        fb.data_msg(1, F_RECORD, [
            ts, hr, cad,
            dist_m * 100,                     # distance scale 100
            int(3.3 * 1000),                  # speed scale 1000
            int((alt_m + 500) * 5),           # altitude scale 5 offset 500
        ])

    # laps: 3 × 1 km @ 5:00 (300s), cadence 86/87/88, hr rising
    fb.define(2, 19, F_LAP)
    for i in range(3):
        ts = t0 + (i + 1) * 300
        fb.data_msg(2, F_LAP, [
            ts,
            300 * 1000,                       # elapsed scale 1000
            1000 * 100,                       # distance scale 100 → 1 km
            145 + i * 5,                      # avg_hr
            160 + i * 5,                      # max_hr
            86 + i,                           # avg_cadence (per-leg)
            8,                                # ascent
        ])

    # session: 3 km, 900s, avg_cadence 88 → _spm = 176 spm (the regression guard)
    fb.define(3, 18, F_SESSION)
    fb.data_msg(3, F_SESSION, [
        t0 + 900, t0,
        900 * 1000,                           # elapsed
        3000 * 100,                           # distance → 3 km
        210,                                  # calories
        150, 170,                             # avg_hr, max_hr
        88,                                   # avg_cadence → ×2 = 176
        24, 22,                               # ascent, descent
    ])

    out = Path(__file__).resolve().parent / "synthetic_activity.fit"
    out.write_bytes(fb.build())
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
