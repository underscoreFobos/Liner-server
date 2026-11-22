import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"edts"}


def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos : pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos, header, size
        pos += size


def _zero_duration(data: bytes, kind: bytes, pos: int, header: int) -> tuple[int, bytes] | None:
    body = pos + header
    version = data[body]
    if kind in (b"mvhd", b"mdhd"):
        at, width = (body + 16, 4) if version == 0 else (body + 24, 8)
    elif kind == b"tkhd":
        at, width = (body + 20, 4) if version == 0 else (body + 28, 8)
    else:
        return None
    if at + width > len(data):
        return None
    return at, bytes(width)


def _edit_patches(data: bytes, pos: int, header: int, size: int) -> list[tuple[int, bytes]]:
    body = pos + header
    version = data[body]
    count = struct.unpack(">I", data[body + 4 : body + 8])[0]
    width = 4 if version == 0 else 8
    entry = width * 2 + 4
    out = []
    at = body + 8
    for _ in range(count):
        if at + entry > pos + size:
            break
        out.append((at, bytes(width)))
        at += entry
    return out


def header_patches(head: bytes) -> list[tuple[int, bytes]]:
    moov = None
    for kind, pos, header, size in _boxes(head, 0, len(head)):
        if kind == b"moov":
            moov = (pos, header, size)
            break
    if not moov:
        return []
    pos, header, size = moov
    children = [kind for kind, *_ in _boxes(head, pos + header, pos + size)]
    if b"mvex" not in children:
        return []
    patches: list[tuple[int, bytes]] = []

    def walk(start: int, end: int) -> None:
        for kind, p, h, s in _boxes(head, start, end):
            if kind in CONTAINERS:
                walk(p + h, p + s)
            elif kind == b"elst":
                patches.extend(_edit_patches(head, p, h, s))
            else:
                patch = _zero_duration(head, kind, p, h)
                if patch:
                    patches.append(patch)

    walk(pos + header, pos + size)
    return patches


def apply(chunk: bytes, offset: int, patches: list[tuple[int, bytes]]) -> bytes:
    end = offset + len(chunk)
    hits = [(at, value) for at, value in patches if at < end and at + len(value) > offset]
    if not hits:
        return chunk
    buf = bytearray(chunk)
    for at, value in hits:
        lo = max(at, offset)
        hi = min(at + len(value), end)
        buf[lo - offset : hi - offset] = value[lo - at : hi - at]
    return bytes(buf)

