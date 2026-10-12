"""
Benchmark for the Resumable Upload Service. Measures what you can honestly
claim on a resume: throughput, per-chunk latency, resume savings, concurrency.

Usage (pip install requests):
    python scripts/benchmark.py throughput https://<app>.azurewebsites.net --size-mb 200
    python scripts/benchmark.py resume     https://<app>.azurewebsites.net --size-mb 200 --fraction 0.5
    python scripts/benchmark.py concurrent https://<app>.azurewebsites.net --size-mb 100 --n 3

Each run creates a fresh throwaway user and deletes its uploads at the end.
The service's own limits apply (see the notes printed with each result).
"""
import argparse
import hashlib
import math
import os
import sys
import threading
import time
import uuid

import requests

MB = 1024 * 1024
TIMEOUT = 120


def fail(msg):
    print(f"\nFAILED: {msg}")
    sys.exit(1)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def pct(vals, p):
    s = sorted(vals)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def new_stats():
    return {"lat": [], "bytes": 0, "rate_limited": 0}


def with_retry(call, st):
    """Run call(); if the server answers 429 (our own rate limit), wait and retry."""
    for _ in range(40):
        r = call()
        if r.status_code != 429:
            return r
        st["rate_limited"] += 1
        time.sleep(3)
    fail("still rate limited after many retries")


def make_user(base):
    st = new_stats()
    name = f"bench_{uuid.uuid4().hex[:8]}"
    pw = "Bench-Pass-123"
    r = with_retry(lambda: requests.post(f"{base}/api/auth/register", timeout=TIMEOUT, json={
        "username": name, "email": f"{name}@example.com", "password": pw}), st)
    if r.status_code != 201:
        fail(f"register -> {r.status_code} {r.text[:200]}")
    r = with_retry(lambda: requests.post(f"{base}/api/auth/login", timeout=TIMEOUT,
                                         data={"username": name, "password": pw}), st)
    if r.status_code != 200:
        fail(f"login -> {r.status_code} {r.text[:200]}")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def split(data, cs):
    return {i + 1: data[i * cs:(i + 1) * cs] for i in range(math.ceil(len(data) / cs))}


def create(base, auth, data, cs, st):
    r = with_retry(lambda: requests.post(f"{base}/api/uploads", headers=auth, timeout=TIMEOUT, json={
        "filename": "bench.bin", "size": len(data), "chunk_size": cs, "file_hash": sha(data)}), st)
    if r.status_code != 201:
        fail(f"create upload -> {r.status_code} {r.text[:200]}")
    return r.json()["upload_id"]


def send(sess, base, auth, uid, n, body, st):
    headers = {**auth, "Content-Type": "application/octet-stream", "X-Checksum": sha(body)}
    for _ in range(40):
        t0 = time.perf_counter()
        r = sess.put(f"{base}/api/uploads/{uid}/chunks/{n}", headers=headers, data=body, timeout=TIMEOUT)
        dt = time.perf_counter() - t0
        if r.status_code == 429:
            st["rate_limited"] += 1
            time.sleep(3)
            continue
        if r.status_code != 200:
            fail(f"chunk {n} -> {r.status_code} {r.text[:200]}")
        st["lat"].append(dt)          # only successful requests are timed
        st["bytes"] += len(body)
        return
    fail(f"chunk {n}: rate limited too many times")


def finish(sess, base, auth, uid, expected_hash):
    t0 = time.perf_counter()
    r = sess.post(f"{base}/api/uploads/{uid}/complete", headers=auth, timeout=TIMEOUT)
    if r.status_code not in (200, 202):
        fail(f"complete -> {r.status_code} {r.text[:200]}")
    while True:
        r = sess.get(f"{base}/api/uploads/{uid}", headers=auth, timeout=TIMEOUT)
        status = r.json()["status"]
        if status in ("completed", "failed"):
            break
        time.sleep(0.5)
    took = time.perf_counter() - t0
    if status != "completed" or r.json()["final_hash"] != expected_hash:
        fail(f"assembly ended as {status} / hash mismatch")
    return took


def cleanup(sess, base, auth, uids):
    for uid in uids:
        sess.delete(f"{base}/api/uploads/{uid}", headers=auth, timeout=TIMEOUT)


def report(title, base, size_mb, cs, st, wall, extra=(), show_req_rate=True):
    n = len(st["lat"])
    total_mb = st["bytes"] / MB
    print(f"\n{'=' * 64}\n{title}\n{'=' * 64}")
    print(f"Target:             {base}")
    print(f"File size / chunk:  {size_mb} MB / {cs // MB} MB   ({n} chunk requests timed)")
    print(f"Wall-clock rate:    {total_mb / wall:.2f} MB/s   (includes any rate-limit waits)")
    if n:
        if show_req_rate:   # meaningless when requests overlap (concurrent mode)
            print(f"Request-time rate:  {total_mb / sum(st['lat']):.2f} MB/s   (time spent inside requests only)")
        print(f"Chunk latency:      p50 {pct(st['lat'], 50) * 1000:.0f} ms | "
              f"p95 {pct(st['lat'], 95) * 1000:.0f} ms | max {max(st['lat']) * 1000:.0f} ms")
    print(f"Rate-limited (429): {st['rate_limited']} retries")
    for line in extra:
        print(line)
    if n < 100:
        print(f"NOTE: only {n} samples, so p95 is rough. Use --size-mb {100 * (cs // MB)} or more for 100+ samples.")
    print("NOTE: numbers depend on your internet connection and distance to the server, and on")
    print("      the default per-IP rate limit (60 chunk uploads/minute). Say so when you quote them.")


def mode_throughput(a):
    base, cs = a.url.rstrip("/"), a.chunk_mb * MB
    data = os.urandom(a.size_mb * MB)
    chunks = split(data, cs)
    auth, sess, st = make_user(base), requests.Session(), new_stats()
    sess.get(f"{base}/health", timeout=TIMEOUT)               # warm-up: wake the app
    uid = create(base, auth, data, cs, st)
    t0 = time.perf_counter()
    for n, body in chunks.items():
        send(sess, base, auth, uid, n, body, st)
    wall = time.perf_counter() - t0
    assembly = finish(sess, base, auth, uid, sha(data))
    cleanup(sess, base, auth, [uid])
    report("THROUGHPUT / LATENCY (single upload, sequential chunks)", base, a.size_mb, cs, st, wall,
           [f"Assembly time:      {assembly:.2f} s   (server joins chunks, verifies SHA-256)"])


def mode_resume(a):
    base, cs = a.url.rstrip("/"), a.chunk_mb * MB
    data = os.urandom(a.size_mb * MB)
    chunks = split(data, cs)
    total = len(chunks)
    k = max(1, min(total - 1, int(total * a.fraction)))
    auth, sess, st = make_user(base), requests.Session(), new_stats()
    sess.get(f"{base}/health", timeout=TIMEOUT)
    uid = create(base, auth, data, cs, st)
    for n in range(1, k + 1):                                  # first part, then 'connection drops'
        send(sess, base, auth, uid, n, chunks[n], st)
    r = sess.get(f"{base}/api/uploads/{uid}", headers=auth, timeout=TIMEOUT)
    missing = r.json()["missing_chunks"]
    if len(missing) != total - k:
        fail(f"server reports {len(missing)} missing, expected {total - k}")
    st2 = new_stats()
    t0 = time.perf_counter()
    for n in missing:                                          # resume: ONLY the missing chunks
        send(sess, base, auth, uid, n, chunks[n], st2)
    wall = time.perf_counter() - t0
    finish(sess, base, auth, uid, sha(data))
    cleanup(sess, base, auth, [uid])
    saved = 100 * (1 - len(missing) / total)
    report("RESUME SAVINGS", base, a.size_mb, cs, st2, wall, [
        f"Interrupted after:  {k} of {total} chunks ({100 * k / total:.0f}%)",
        f"Re-sent on resume:  {len(missing)} chunks ({100 * len(missing) / total:.0f}% of the file)",
        f"Restart-from-zero:  would re-send {total} chunks (100%)",
        f"Data NOT re-sent:   {saved:.0f}%   (compared with a non-resumable upload restarting from zero)",
        "Final file hash verified: OK"])


def mode_concurrent(a):
    base, cs = a.url.rstrip("/"), a.chunk_mb * MB
    if a.n > 3:
        fail("by default one user may run at most 3 uploads at once (MAX_ACTIVE_UPLOADS_PER_USER); use --n 3 or fewer")
    auth, probe = make_user(base), requests.Session()
    probe.get(f"{base}/health", timeout=TIMEOUT)
    results, errors, uids = [], [], []
    lock = threading.Lock()

    def worker():
        try:
            data = os.urandom(a.size_mb * MB)
            chunks, sess, st = split(data, cs), requests.Session(), new_stats()
            uid = create(base, auth, data, cs, st)
            with lock:
                uids.append(uid)
            for n, body in chunks.items():
                send(sess, base, auth, uid, n, body, st)
            finish(sess, base, auth, uid, sha(data))
            with lock:
                results.append(st)
        except SystemExit:
            with lock:
                errors.append("worker failed (see message above)")

    threads = [threading.Thread(target=worker) for _ in range(a.n)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    cleanup(probe, base, auth, uids)
    if errors or len(results) != a.n:
        fail(f"{len(errors)} of {a.n} concurrent uploads failed")
    merged = new_stats()
    for s in results:
        merged["lat"] += s["lat"]
        merged["bytes"] += s["bytes"]
        merged["rate_limited"] += s["rate_limited"]
    report(f"CONCURRENCY ({a.n} uploads at once, same user)", base, a.size_mb, cs, merged, wall, [
        f"Result:             {a.n} of {a.n} uploads completed and hash-verified, 0 errors",
        "NOTE: capped at 3 per user by design; more needs several users (registration is limited to 3/min per IP)."],
        show_req_rate=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["throughput", "resume", "concurrent"])
    p.add_argument("url")
    p.add_argument("--size-mb", type=int, default=100, help="file size in MB (user storage quota is 1 GB by default)")
    p.add_argument("--chunk-mb", type=int, default=5, help="chunk size in MB (server allows 1-50)")
    p.add_argument("--fraction", type=float, default=0.5, help="resume mode: fraction uploaded before the 'drop'")
    p.add_argument("--n", type=int, default=3, help="concurrent mode: parallel uploads (max 3)")
    args = p.parse_args()
    {"throughput": mode_throughput, "resume": mode_resume, "concurrent": mode_concurrent}[args.mode](args)