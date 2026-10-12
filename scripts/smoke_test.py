"""
End-to-end smoke test for a deployed Resumable Upload Service.

Usage:
    python smoke_test.py https://<your-app>.azurewebsites.net

Needs:  pip install requests

It registers a throwaway user, uploads a 3 MB file in 1 MB chunks, deliberately
"drops" chunk 2 to show the server reports it as missing, resumes by sending
only that chunk, assembles the file, and downloads it back to verify the
SHA-256 matches. Nothing here needs any secrets.
"""
import hashlib
import os
import sys
import time
import uuid

import requests

if len(sys.argv) != 2:
    sys.exit("Usage: python smoke_test.py https://<your-app>.azurewebsites.net")

BASE = sys.argv[1].rstrip("/")
API = f"{BASE}/api"
CHUNK = 1024 * 1024  # 1 MB (the server's minimum chunk size)
TIMEOUT = 120        # generous: the first request can wake a sleeping app


def step(msg):
    print(f"\n==> {msg}")


def check(resp, expected, what):
    if resp.status_code not in (expected if isinstance(expected, tuple) else (expected,)):
        print(f"FAILED: {what} -> HTTP {resp.status_code}\n{resp.text[:500]}")
        sys.exit(1)
    return resp


# ── 0. Health ──────────────────────────────────────────────────────────────────
step("Health check")
r = check(requests.get(f"{BASE}/health", timeout=TIMEOUT), 200, "health")
print(r.json())

# ── 1. Register + login ────────────────────────────────────────────────────────
username = f"smoke_{uuid.uuid4().hex[:8]}"
password = "Smoke-Test-Pass-1"
step(f"Register user {username}")
check(requests.post(f"{API}/auth/register", timeout=TIMEOUT, json={
    "username": username, "email": f"{username}@example.com", "password": password,
}), 201, "register")

step("Log in")
r = check(requests.post(f"{API}/auth/login", timeout=TIMEOUT,
                        data={"username": username, "password": password}), 200, "login")
auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

# ── 2. Create a 3 MB file and an upload session ────────────────────────────────
data = os.urandom(CHUNK * 3)
file_hash = hashlib.sha256(data).hexdigest()
chunks = {i + 1: data[i * CHUNK:(i + 1) * CHUNK] for i in range(3)}

step("Create upload session (3 chunks)")
r = check(requests.post(f"{API}/uploads", headers=auth, timeout=TIMEOUT, json={
    "filename": "smoke-test.bin", "size": len(data),
    "chunk_size": CHUNK, "file_hash": file_hash,
}), 201, "create upload")
upload_id = r.json()["upload_id"]
print("upload_id:", upload_id, "| total_chunks:", r.json()["total_chunks"])


def put_chunk(n):
    return check(requests.put(
        f"{API}/uploads/{upload_id}/chunks/{n}", timeout=TIMEOUT,
        headers={**auth, "Content-Type": "application/octet-stream",
                 "X-Checksum": hashlib.sha256(chunks[n]).hexdigest()},
        data=chunks[n]), 200, f"upload chunk {n}")


# ── 3. Upload chunks 1 and 3 only (simulate an interruption) ───────────────────
step("Upload chunks 1 and 3 (chunk 2 'lost' to a dropped connection)")
for n in (1, 3):
    print(f"chunk {n}:", put_chunk(n).json()["status"])

step("Ask the server what is missing")
r = check(requests.get(f"{API}/uploads/{upload_id}", headers=auth, timeout=TIMEOUT), 200, "status")
missing = r.json()["missing_chunks"]
print("missing_chunks:", missing, "| progress:", r.json()["progress"], "%")
assert missing == [2], f"expected [2], got {missing}"

# ── 4. A corrupted chunk must be rejected without harming the upload ───────────
step("Send chunk 2 with a WRONG checksum (must be rejected, upload must survive)")
r = requests.put(f"{API}/uploads/{upload_id}/chunks/2", timeout=TIMEOUT,
                 headers={**auth, "Content-Type": "application/octet-stream",
                          "X-Checksum": "0" * 64}, data=chunks[2])
print("HTTP", r.status_code, "-", r.json().get("detail", "")[:60])
assert r.status_code == 400
r = check(requests.get(f"{API}/uploads/{upload_id}", headers=auth, timeout=TIMEOUT), 200, "status")
assert r.json()["status"] == "uploading" and r.json()["uploaded_chunks"] == 2

# ── 5. Resume: send only the missing chunk ─────────────────────────────────────
step("Resume: send ONLY chunk 2")
print("chunk 2:", put_chunk(2).json()["status"])

# ── 6. Assemble ────────────────────────────────────────────────────────────────
step("Complete (background assembly)")
check(requests.post(f"{API}/uploads/{upload_id}/complete", headers=auth, timeout=TIMEOUT),
      (200, 202), "complete")

for _ in range(60):
    r = check(requests.get(f"{API}/uploads/{upload_id}", headers=auth, timeout=TIMEOUT), 200, "poll")
    status = r.json()["status"]
    print("status:", status)
    if status in ("completed", "failed"):
        break
    time.sleep(2)
assert status == "completed", f"assembly ended as {status}"
assert r.json()["final_hash"] == file_hash, "server-side hash does not match"
print("server-side SHA-256 matches the original file")

# ── 7. Download and verify ─────────────────────────────────────────────────────
step("Download the file back and verify it byte for byte")
r = check(requests.get(f"{API}/uploads/{upload_id}/download", headers=auth,
                       timeout=TIMEOUT, allow_redirects=True), 200, "download")
assert hashlib.sha256(r.content).hexdigest() == file_hash, "downloaded file differs!"
print(f"downloaded {len(r.content)} bytes, SHA-256 matches")

# ── 8. Clean up ────────────────────────────────────────────────────────────────
step("Delete the test upload")
check(requests.delete(f"{API}/uploads/{upload_id}", headers=auth, timeout=TIMEOUT), 200, "delete")

print("\nALL CHECKS PASSED")