"""Control agent for AgentCore Runtime: allocate memory, hold it, free it, respond.

No Strands, no boto3 client, no model call. It tests the documented V2 behaviour
("reclaims memory as your agent releases it") directly: the process really uses N MB
during the request, then really gives it back before it responds.

Payload: {"prompt": "...", "alloc_mb": N, "hold_s": S}. Without alloc_mb (or 0) it just
echoes. Response shape matches agent_echo/main.py, plus the allocation fields.

Local self-test, no bedrock_agentcore needed:  python agent_alloc/main.py --selftest 64
"""
import ctypes
import gc
import mmap
import os
import sys
import time

try:
    from bedrock_agentcore.runtime import BedrockAgentCoreApp
except ImportError:  # lets the allocation helper run standalone (--selftest)
    BedrockAgentCoreApp = None

MB = 1024 * 1024
DEFAULT_HOLD_S = 5
MAX_HOLD_S = 600


def rss_mb():
    """Resident set size in MB from /proc/self/status, or None where /proc is absent."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB -> MB
    except (OSError, ValueError, IndexError):
        pass
    return _win_working_set_mb()


def _win_working_set_mb():
    """Dev-machine fallback so --selftest shows numbers on Windows (never used on the runtime)."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

    c = Counters()
    c.cb = ctypes.sizeof(c)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
        return None
    return round(c.WorkingSetSize / MB, 1)


def trim_heap():
    """Ask glibc to return free heap pages to the OS. No-op on musl/Windows/macOS."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def allocate_and_free(alloc_mb, hold_s=DEFAULT_HOLD_S):
    """Hold alloc_mb of resident memory for hold_s seconds, free it, return RSS readings.

    An anonymous mmap is used instead of bytearray/list: closing it munmaps the pages,
    so they definitely go back to the OS. A bytearray goes through the allocator, which
    may keep freed memory in the process (the "never frees" behaviour this control
    is meant to avoid). Every page is written, otherwise it would never become resident.
    """
    before = rss_mb()
    size = alloc_mb * MB
    buf = mmap.mmap(-1, size)
    chunk = b"\x01" * MB  # non-zero data, written to every page
    for _ in range(alloc_mb):
        buf.write(chunk)
    peak_after_touch = rss_mb()
    time.sleep(hold_s)
    peak = max(x for x in (peak_after_touch, rss_mb()) if x is not None) \
        if peak_after_touch is not None else None
    buf.close()  # munmap: pages are returned to the OS here
    del buf, chunk
    gc.collect()
    trim_heap()
    return {"alloc_mb": alloc_mb, "rss_mb_before": before, "rss_mb_peak": peak, "rss_mb_after": rss_mb()}


def handle(payload):
    payload = payload or {}
    out = {
        "result": payload.get("prompt"),
        "usage": {"input_tokens": None, "output_tokens": None},
        "variant": os.environ.get("VARIANT_ID"),
    }
    alloc_mb = int(payload.get("alloc_mb") or 0)
    if alloc_mb > 0:
        hold_s = min(max(float(payload.get("hold_s", DEFAULT_HOLD_S)), 0), MAX_HOLD_S)
        out.update(allocate_and_free(alloc_mb, hold_s))
    return out


if BedrockAgentCoreApp is not None:
    app = BedrockAgentCoreApp()
    app.entrypoint(handle)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        mb = int(sys.argv[2]) if len(sys.argv) > 2 else 64
        print(handle({"prompt": "selftest", "alloc_mb": mb, "hold_s": 1}))
    elif BedrockAgentCoreApp is None:
        sys.exit("bedrock_agentcore is not installed")
    else:
        app.run()
