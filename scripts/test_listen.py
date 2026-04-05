#!/usr/bin/env python3
"""Send a FLAC/WAV/MP3 file to the /listen endpoint in chunks, simulating the Android client.
No dependencies beyond Python stdlib + ffmpeg.
"""
import argparse
import io
import struct
import subprocess
import time
import urllib.request

CHUNK_SECONDS = 10
SAMPLE_RATE = 44100
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit


def decode_audio(filepath: str) -> bytes:
    """Decode audio file to raw PCM using ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-i", filepath, "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()[-200:]}")
    return result.stdout


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM in a WAV header."""
    buf = io.BytesIO()
    data_size = len(pcm)
    # WAV header
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, CHANNELS, SAMPLE_RATE,
                          SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH, CHANNELS * SAMPLE_WIDTH,
                          SAMPLE_WIDTH * 8))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm)
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description="Send audio to WaxID /listen endpoint")
    parser.add_argument("file", help="Audio file (FLAC, WAV, MP3)")
    parser.add_argument("--server", default="http://localhost:8457", help="Server URL")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="Seconds between chunks (Android sends every 3s)")
    parser.add_argument("--start", type=float, default=0.0, help="Start offset in seconds")
    args = parser.parse_args()

    print(f"Decoding {args.file}...")
    pcm = decode_audio(args.file)
    total_samples = len(pcm) // SAMPLE_WIDTH
    duration = total_samples / SAMPLE_RATE

    chunk_bytes = CHUNK_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH
    start_byte = int(args.start * SAMPLE_RATE * SAMPLE_WIDTH)
    offset = start_byte

    print(f"Sending to {args.server}/listen")
    print(f"Duration: {duration:.1f}s, chunk: {CHUNK_SECONDS}s, interval: {args.interval}s")

    while offset < len(pcm):
        chunk_pcm = pcm[offset:offset + chunk_bytes]
        wav_bytes = pcm_to_wav(chunk_pcm)
        pos_s = offset / (SAMPLE_RATE * SAMPLE_WIDTH)
        try:
            req = urllib.request.Request(
                f"{args.server}/listen",
                data=wav_bytes,
                headers={
                    "Content-Type": "audio/wav",
                    "x-recorded-at": str(time.time()),
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                print(f"[{pos_s:6.1f}s] sent {len(wav_bytes)} bytes → {resp.status}")
        except Exception as e:
            print(f"[{pos_s:6.1f}s] error: {e}")
        offset += chunk_bytes
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
