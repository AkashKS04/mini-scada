#!/usr/bin/env python3
"""
lib/dnp3lite.py - a compact subset of DNP3 (IEEE 1815)

Implemented
    Data link layer    frames, CRC-16/DNP on the header and every 16-byte block
    Transport layer    single and multi-segment reassembly
    Application layer  SELECT / OPERATE / DIRECT OPERATE with CROB (g12 v1),
                       responses with IIN bits and a status per point
    Roles              Master and Outstation over TCP

NOT implemented (so this is a subset, not a full DNP3 stack)
    Class polls, event buffers, unsolicited responses, time sync, file
    transfer, confirmed link messages and Secure Authentication.

Addressing used by this project
    Master link address            1
    Outstation link address        100 + substation number (SS-01 -> 101)
    Control point index            0 = transformer breaker, N = feeder N breaker

Run this file directly to self-test the CRC against the standard check value.
"""

import socket
import struct
import threading
import time

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────
START = b"\x05\x64"
LINK_UNCONFIRMED_USER_DATA = 0x04

FC_SELECT, FC_OPERATE, FC_DIRECT_OPERATE, FC_RESPONSE = 0x03, 0x04, 0x05, 0x81

IIN2_NO_FUNC_CODE_SUPPORT = 0x01
IIN2_OBJECT_UNKNOWN       = 0x02
IIN2_PARAMETER_ERROR      = 0x04

# Control relay output block: trip/close code (bits 7-6) + op type (bits 3-0)
CC_PULSE_ON_TRIP  = 0x81     # OPEN the breaker
CC_PULSE_ON_CLOSE = 0x41     # CLOSE the breaker
CMD_BY_CODE = {CC_PULSE_ON_TRIP: "OPEN", CC_PULSE_ON_CLOSE: "CLOSE"}
CODE_BY_CMD = {"OPEN": CC_PULSE_ON_TRIP, "CLOSE": CC_PULSE_ON_CLOSE}

ST_SUCCESS, ST_TIMEOUT, ST_NO_SELECT, ST_FORMAT_ERROR = 0, 1, 2, 3
ST_NOT_SUPPORTED, ST_ALREADY_ACTIVE, ST_HARDWARE_ERROR = 4, 5, 6
STATUS_NAMES = {0: "SUCCESS", 1: "TIMEOUT", 2: "NO_SELECT", 3: "FORMAT_ERROR",
                4: "NOT_SUPPORTED", 5: "ALREADY_ACTIVE", 6: "HARDWARE_ERROR"}


class ProtocolError(Exception):
    def __init__(self, message, iin2=IIN2_PARAMETER_ERROR):
        super().__init__(message)
        self.iin2 = iin2


# ─────────────────────────────────────────────
# CRC-16/DNP
# ─────────────────────────────────────────────
def crc16(data):
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA6BC if crc & 1 else crc >> 1
    return ~crc & 0xFFFF


# ─────────────────────────────────────────────
# Data link layer
# ─────────────────────────────────────────────
def build_link_frame(control, dest, src, user_data):
    assert len(user_data) <= 250
    header = START + bytes([5 + len(user_data), control]) + struct.pack("<HH", dest, src)
    out = bytearray(header + struct.pack("<H", crc16(header)))
    for i in range(0, len(user_data), 16):
        block = user_data[i:i + 16]
        out += block + struct.pack("<H", crc16(block))
    return bytes(out)


class FrameParser:
    """Turns a TCP byte stream into link frames, dropping any with a bad CRC."""

    def __init__(self):
        self.buf = bytearray()
        self.crc_errors = 0

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            i = self.buf.find(START)
            if i < 0:
                del self.buf[:max(0, len(self.buf) - 1)]   # keep a lone 0x05
                break
            if i:
                del self.buf[:i]
            if len(self.buf) < 10:
                break
            length = self.buf[2]
            if length < 5 or crc16(bytes(self.buf[:8])) != struct.unpack("<H", self.buf[8:10])[0]:
                self.crc_errors += 1
                del self.buf[:2]
                continue
            n = length - 5
            total = 10 + n + 2 * ((n + 15) // 16)
            if len(self.buf) < total:
                break

            payload, pos, remaining, ok = bytearray(), 10, n, True
            while remaining > 0:
                blk = min(16, remaining)
                chunk = bytes(self.buf[pos:pos + blk])
                if crc16(chunk) != struct.unpack("<H", self.buf[pos + blk:pos + blk + 2])[0]:
                    ok = False
                    break
                payload += chunk
                pos += blk + 2
                remaining -= blk

            ctl = self.buf[3]
            if ok:
                frames.append({
                    "dir": bool(ctl & 0x80), "prm": bool(ctl & 0x40), "fc": ctl & 0x0F,
                    "dest": struct.unpack("<H", self.buf[4:6])[0],
                    "src":  struct.unpack("<H", self.buf[6:8])[0],
                    "data": bytes(payload),
                })
            else:
                self.crc_errors += 1
            del self.buf[:total]
        return frames


# ─────────────────────────────────────────────
# Transport layer
# ─────────────────────────────────────────────
def to_segments(app, seq):
    """Split an application fragment into transport segments (249 bytes each)."""
    chunks = [app[i:i + 249] for i in range(0, len(app), 249)] or [b""]
    segs = []
    for n, chunk in enumerate(chunks):
        hdr = (0x80 if n == len(chunks) - 1 else 0) | (0x40 if n == 0 else 0) | (seq & 0x3F)
        segs.append(bytes([hdr]) + chunk)
        seq = (seq + 1) & 0x3F
    return segs, seq


class Reassembler:
    def __init__(self):
        self.buf = None

    def add(self, seg):
        if not seg:
            return None
        if seg[0] & 0x40:
            self.buf = bytearray()
        if self.buf is None:
            return None
        self.buf += seg[1:]
        if seg[0] & 0x80:
            out, self.buf = bytes(self.buf), None
            return out
        return None


# ─────────────────────────────────────────────
# Application layer: CROB (group 12, variation 1)
# ─────────────────────────────────────────────
def build_crob_block(qual, points):
    """points: (index, code, count, on_ms, off_ms, status) tuples."""
    isz = 1 if qual == 0x17 else 2
    out = bytes([12, 1, qual]) + (bytes([len(points)]) if isz == 1 else struct.pack("<H", len(points)))
    for idx, code, count, on, off, status in points:
        out += idx.to_bytes(isz, "little") + struct.pack("<BBIIB", code, count, on, off, status)
    return out


def parse_crob_block(body):
    """Returns (qualifier, points). Raises ProtocolError on anything unsupported."""
    if len(body) < 4:
        raise ProtocolError("object header too short")
    if (body[0], body[1]) != (12, 1):
        raise ProtocolError("only CROB (g12 v1) is supported", IIN2_OBJECT_UNKNOWN)
    qual = body[2]
    if qual == 0x17:
        n, pos, isz = body[3], 4, 1
    elif qual == 0x28:
        n, pos, isz = struct.unpack("<H", body[3:5])[0], 5, 2
    else:
        raise ProtocolError("unsupported qualifier 0x%02X" % qual)
    points = []
    for _ in range(n):
        if len(body) < pos + isz + 11:
            raise ProtocolError("truncated CROB")
        idx = int.from_bytes(body[pos:pos + isz], "little")
        pos += isz
        points.append((idx,) + struct.unpack("<BBIIB", body[pos:pos + 11]))
        pos += 11
    return qual, points


def build_crob_request(fc, seq, points):
    """points: (index, code, count, on_ms, off_ms) tuples. FIR+FIN set."""
    return bytes([0xC0 | (seq & 0x0F), fc]) + build_crob_block(
        0x17, [p + (0,) for p in points])


def build_response(seq, iin1=0, iin2=0, objects=b""):
    return bytes([0xC0 | (seq & 0x0F), FC_RESPONSE, iin1, iin2]) + objects


# ─────────────────────────────────────────────
# Point map: (outstation address, index) <-> slave id
# ─────────────────────────────────────────────
def point_maps(config):
    by_slave, by_point = {}, {}
    for ss in config["substations"]:
        addr = 100 + int(ss["id"].split("-")[1])
        points = [(0, ss["transformer"]["slave_id"])]
        points += [(f["feeder_number"], f["slave_id"]) for f in ss["feeders"]]
        for idx, slave in points:
            by_slave[slave] = (addr, idx)
            by_point[(addr, idx)] = slave
    return by_slave, by_point


# ─────────────────────────────────────────────
# Outstation (runs next to the simulated RTUs)
# ─────────────────────────────────────────────
class Outstation:
    def __init__(self, points, apply_fn, host="127.0.0.1", port=20000,
                 select_timeout=10.0, log=print):
        self.points = points            # {(address, index): slave_id}
        self.apply_fn = apply_fn        # (slave_id, "OPEN"|"CLOSE") -> (ok, message)
        self.host, self.port = host, port
        self.select_timeout = select_timeout
        self.addresses = {a for a, _ in points}
        self.log = log

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(5)
        self.log(f"[DNP3] Outstation listening on {self.host}:{self.port} "
                 f"(addresses {sorted(self.addresses)})")
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        parser, reasm, selections, tseq = FrameParser(), {}, {}, 0
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                for fr in parser.feed(data):
                    if not (fr["prm"] and fr["fc"] == LINK_UNCONFIRMED_USER_DATA
                            and fr["dest"] in self.addresses):
                        continue
                    key = (fr["src"], fr["dest"])
                    app = reasm.setdefault(key, Reassembler()).add(fr["data"])
                    if app is None:
                        continue
                    resp = self._handle(fr["src"], fr["dest"], app, selections)
                    segs, tseq = to_segments(resp, tseq)
                    for seg in segs:
                        conn.sendall(build_link_frame(0x44, fr["src"], fr["dest"], seg))

    # -- application layer ---------------------------------------------
    def _validate(self, addr, pts):
        out = []
        for idx, code, count, _on, _off, _st in pts:
            if (addr, idx) not in self.points or code not in CMD_BY_CODE:
                out.append(ST_NOT_SUPPORTED)
            elif count != 1:
                out.append(ST_FORMAT_ERROR)
            else:
                out.append(ST_SUCCESS)
        return out

    def _execute(self, addr, pts):
        out = []
        for idx, code, _count, _on, _off, _st in pts:
            ok, msg = self.apply_fn(self.points[(addr, idx)], CMD_BY_CODE[code])
            out.append(ST_SUCCESS if ok else
                       ST_ALREADY_ACTIVE if msg.startswith("already") else ST_HARDWARE_ERROR)
        return out

    def _handle(self, master, addr, app, selections):
        if len(app) < 2:
            return build_response(0, 0, IIN2_PARAMETER_ERROR)
        ac, fc = app[0], app[1]
        seq = ac & 0x0F
        if fc not in (FC_SELECT, FC_OPERATE, FC_DIRECT_OPERATE):
            return build_response(seq, 0, IIN2_NO_FUNC_CODE_SUPPORT)
        if (ac & 0xC0) != 0xC0:
            return build_response(seq, 0, IIN2_PARAMETER_ERROR)   # multi-fragment requests not supported
        try:
            qual, pts = parse_crob_block(app[2:])
        except ProtocolError as e:
            return build_response(seq, 0, e.iin2)

        sel_key = (master, addr)
        ident = tuple(p[:5] for p in pts)
        statuses = self._validate(addr, pts)
        ok = all(s == ST_SUCCESS for s in statuses)

        if fc == FC_SELECT:
            if ok:
                selections[sel_key] = {"seq": seq, "ident": ident, "t": time.time()}
            self.log(f"[DNP3] SELECT   addr={addr} points={[p[0] for p in pts]} -> {statuses}")

        elif fc == FC_OPERATE:
            sel = selections.pop(sel_key, None)
            if sel is None or sel["ident"] != ident or seq != (sel["seq"] + 1) & 0x0F:
                statuses = [ST_NO_SELECT] * len(pts)
            elif time.time() - sel["t"] > self.select_timeout:
                statuses = [ST_TIMEOUT] * len(pts)
            elif ok:
                statuses = self._execute(addr, pts)
            self.log(f"[DNP3] OPERATE  addr={addr} points={[p[0] for p in pts]} -> {statuses}")

        else:  # DIRECT OPERATE
            if ok:
                statuses = self._execute(addr, pts)
            self.log(f"[DNP3] DIRECT   addr={addr} points={[p[0] for p in pts]} -> {statuses}")

        echoed = [p[:5] + (s,) for p, s in zip(pts, statuses)]
        return build_response(seq, 0, 0, build_crob_block(qual, echoed))


# ─────────────────────────────────────────────
# Master (used by the control service)
# ─────────────────────────────────────────────
class Master:
    def __init__(self, host="127.0.0.1", port=20000, address=1, timeout=5.0):
        self.host, self.port, self.address, self.timeout = host, port, address, timeout
        self.sock = None
        self.tseq = self.aseq = 0

    @property
    def connected(self):
        return self.sock is not None

    def connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.parser, self.reasm = FrameParser(), Reassembler()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _exchange(self, dest, app):
        segs, self.tseq = to_segments(app, self.tseq)
        for seg in segs:
            self.sock.sendall(build_link_frame(0xC4, dest, self.address, seg))
        deadline = time.time() + self.timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("no response from outstation")
            self.sock.settimeout(left)
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("outstation closed the connection")
            for fr in self.parser.feed(data):
                if fr["src"] == dest and fr["fc"] == LINK_UNCONFIRMED_USER_DATA:
                    resp = self.reasm.add(fr["data"])
                    if resp is not None:
                        return resp

    @staticmethod
    def _check(resp, seq, index):
        if len(resp) < 4:
            return False, "short response"
        ac, fc, _iin1, iin2 = resp[0], resp[1], resp[2], resp[3]
        if fc != FC_RESPONSE:
            return False, "unexpected function 0x%02X" % fc
        if ac & 0x0F != seq:
            return False, "sequence mismatch"
        if iin2:
            return False, "outstation IIN2=0x%02X" % iin2
        try:
            _q, pts = parse_crob_block(resp[4:])
        except ProtocolError as e:
            return False, "unparseable response: %s" % e
        for p in pts:
            if p[0] == index:
                return p[-1] == ST_SUCCESS, STATUS_NAMES.get(p[-1], str(p[-1]))
        return False, "point missing in response"

    def operate(self, dest, index, command):
        """SELECT then OPERATE one breaker. Returns (ok, message)."""
        point = (index, CODE_BY_CMD[command], 1, 1000, 0)
        for fc, name in ((FC_SELECT, "SELECT"), (FC_OPERATE, "OPERATE")):
            seq = self.aseq
            self.aseq = (self.aseq + 1) & 0x0F
            resp = self._exchange(dest, build_crob_request(fc, seq, [point]))
            ok, msg = self._check(resp, seq, index)
            if not ok:
                return False, f"{name} refused: {msg}"
        return True, "SELECT + OPERATE confirmed"


if __name__ == "__main__":
    check = crc16(b"123456789")
    print("CRC-16/DNP of '123456789' = 0x%04X (standard check value 0xEA82) -> %s"
          % (check, "OK" if check == 0xEA82 else "FAIL"))
