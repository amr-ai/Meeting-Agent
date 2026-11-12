"""Single-thread-owned H264 decode and JPEG preparation using existing PyAV."""
import base64
import hashlib
import re
from fractions import Fraction


def jpeg(frame, max_edge=1920):
    import av
    scale = min(1, max_edge / max(frame.width, frame.height))
    width, height = max(2, int(frame.width * scale) // 2 * 2), max(2, int(frame.height * scale) // 2 * 2)
    frame = frame.reformat(width=width, height=height, format='yuvj420p')
    encoder = av.CodecContext.create('mjpeg', 'w')
    encoder.width, encoder.height, encoder.pix_fmt = width, height, 'yuvj420p'
    encoder.time_base, encoder.thread_count = Fraction(1, 1), 1
    frame.pts = 0
    data = b''.join(bytes(p) for p in [*encoder.encode(frame), *encoder.encode(None)])
    return data, width, height


def image_frame(data, max_pixels):
    import av
    decoder = av.CodecContext.create('mjpeg', 'r')
    decoder.options = {'max_pixels': str(max_pixels)}
    frames = decoder.decode(av.Packet(data))
    if not frames or frames[0].width * frames[0].height > max_pixels:
        raise ValueError('Unsupported or oversized image')
    return frames[0]


def crop_image(data, box, max_pixels, max_edge):
    import av
    frame = image_frame(data, max_pixels)
    if box:
        left, top, right, bottom = box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ValueError('Crop must be normalized left,top,right,bottom')
        pixels = frame.to_ndarray(format='rgb24')
        pixels = pixels[int(top * frame.height):int(bottom * frame.height),
                        int(left * frame.width):int(right * frame.width)]
        if min(pixels.shape[:2]) < 2:
            raise ValueError('Crop is too small')
        frame = av.VideoFrame.from_ndarray(pixels.copy(), format='rgb24')
    return jpeg(frame, max_edge)[0]


class Decoder:
    def __init__(self, max_pixels=4000000, max_edge=1920):
        self.max_pixels, self.max_edge = max_pixels, max_edge
        self.parameters = {}
        self.reset()

    def reset(self):
        import av
        self.codec = av.CodecContext.create('h264', 'r')
        self.codec.thread_count = 1
        self.codec.options = {'max_pixels': str(self.max_pixels)}
        self.needs_keyframe = True
        self.pending = bytearray()

    def decode(self, encoded, relative, emit=True):
        import av
        raw = base64.b64decode(encoded, validate=True)
        if not raw:
            return None
        # Recall supplies raw H264. Also accept complete four-byte-length-prefixed
        # NAL units when no Annex-B start codes are present; reject malformed data.
        if not re.search(b'\x00\x00\x01', raw) and len(raw) >= 4:
            offset, units = 0, []
            while offset + 4 <= len(raw):
                size = int.from_bytes(raw[offset:offset + 4], 'big')
                offset += 4
                if size <= 0 or offset + size > len(raw):
                    units = []
                    break
                units.append(b'\x00\x00\x00\x01' + raw[offset:offset + size])
                offset += size
            if units and offset == len(raw):
                raw = b''.join(units)
            # Otherwise it may be the continuation of an Annex-B packet. The
            # codec parser handles split NAL units; don't drop their payload.
        if self.needs_keyframe:
            self.pending.extend(raw)
            if len(self.pending) > 1048576:
                self.pending.clear()
                return None
            raw = bytes(self.pending)
        starts = list(re.finditer(b'\x00\x00\x00\x01|\x00\x00\x01', raw))
        types = set()
        for index, marker in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(raw)
            unit = raw[marker.end():end]
            if not unit:
                continue
            kind = unit[0] & 31
            types.add(kind)
            if kind in {7, 8} and len(unit) < 65536:
                self.parameters[kind] = b'\x00\x00\x00\x01' + unit
        if self.needs_keyframe:
            if 5 not in types:
                return None
            raw = b''.join(self.parameters.get(kind, b'') for kind in (7, 8)) + raw
            self.pending.clear()
            self.needs_keyframe = False
        latest = None
        try:
            for packet in self.codec.parse(raw):
                packet.pts = int(relative * 1000000)
                packet.time_base = Fraction(1, 1000000)
                for frame in self.codec.decode(packet):
                    if frame.width * frame.height > self.max_pixels:
                        raise ValueError('Video exceeds configured pixel limit')
                    latest = frame
        except av.error.FFmpegError:
            self.reset()
            raise ValueError('H264 decode failed; awaiting keyframe') from None
        if latest is None or not emit:
            return None
        thumb = latest.reformat(width=32, height=18, format='gray').to_ndarray().tobytes()
        # Hash visible pixels, not JPEG encoder metadata.
        digest = hashlib.sha256(latest.to_ndarray(format='rgb24').tobytes()).hexdigest()
        data, width, height = jpeg(latest, self.max_edge)
        return {'jpeg': data, 'width': width, 'height': height, 'fingerprint': thumb,
                'digest': digest, 'decoded_pts': float(latest.pts * latest.time_base)
                if latest.pts is not None and latest.time_base is not None else None}
