# 📦 Resumable File Upload Service

[![CI](https://github.com/your-username/resumable-upload-service/actions/workflows/ci.yml/badge.svg)](https://github.com/your-username/resumable-upload-service/actions)
![Python](https://img.shields.io/badge/python-3.11%20|%203.12-blue)
![Tests](https://img.shields.io/badge/tests-28%20passed-brightgreen)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688)

# 📦 Resumable File Upload Service

> **Upload large files with confidence** — pause, resume, and never lose progress.

---

**Resumable File Upload Service** enables users to upload large files reliably without worrying about interruptions or failures. It allows uploads to be paused and resumed from where they left off, saving time and bandwidth. The service manages file storage, tracks upload progress, and ensures files are complete and uncorrupted before finalizing. It handles all the complexity of chunked uploads so users can focus on their content, not the technical hurdles.

---

It includes:

- JWT authentication
- Resumable chunk-based uploads
- Chunk-level idempotency
- SHA-256 integrity verification
- Upload expiration and cleanup
- Per-user storage quotas
- Global storage quotas
- Daily upload limits
- Concurrent upload limits
- Per-IP rate limiting
- Admin routes/dashboard
- SQLite for simple local development
- PostgreSQL-compatible database configuration
- Local filesystem storage for development
- Docker support
- Automated tests

---

# 🏗️ Architecture

```text
                         ┌──────────────────────────┐
                         │      Browser / Client     │
                         │                          │
                         │  • Authentication        │
                         │  • File chunking         │
                         │  • SHA-256 hashing       │
                         │  • Resume interrupted    │
                         │    uploads               │
                         └────────────┬─────────────┘
                                      │
                                      │ HTTP / HTTPS
                                      ▼
                    ┌─────────────────────────────────┐
                    │          FastAPI Application     │
                    │                                 │
                    │  ┌──────────┐  ┌─────────────┐ │
                    │  │  Auth    │  │   Uploads   │ │
                    │  │  Routes  │  │   Routes    │ │
                    │  └──────────┘  └─────────────┘ │
                    │                                 │
                    │  ┌─────────────────────────────┐│
                    │  │        Middleware            ││
                    │  │                             ││
                    │  │ • JWT Authentication        ││
                    │  │ • Rate Limiting             ││
                    │  │ • CORS                      ││
                    │  │ • Request IDs               ││
                    │  │ • Structured Logging        ││
                    │  └─────────────────────────────┘│
                    └──────────────┬──────────┬───────┘
                                   │          │
                     ┌─────────────┘          └─────────────┐
                     ▼                                      ▼
          ┌────────────────────┐                 ┌──────────────────────┐
          │      Database      │                 │    Storage Layer     │
          │                    │                 │                      │
          │ SQLite (local)     │                 │ Local Disk (dev)     │
          │ PostgreSQL (prod)  │                 │         OR           │
          │                    │                 │ Azure Blob (prod)    │
          │ • users            │                 │                      │
          │ • uploads          │                 │ • chunks             │
          │ • upload_chunks    │                 │ • completed files    │
          └────────────────────┘                 └──────────────────────┘
                                   │
                                   ▼
                         ┌────────────────────┐
                         │ Cleanup Worker     │
                         │                    │
                         │ Periodically finds │
                         │ expired uploads    │
                         │ and removes their  │
                         │ storage + DB data  │
                         └────────────────────┘

## Run locally


### 1. Clone

git clone <repository-url>
cd resumable-upload-service

### 2. Create environment

python -m venv .venv

### 3. Activate

Windows:

.venv\Scripts\activate

### 4. Install dependencies

pip install -r requirements.txt

### 5. Configure environment

Copy `.env.example` to `.env`.

Generate a secret:

python -c "import secrets; print(secrets.token_hex(32))"

Put the generated value into `SECRET_KEY`.

### 6. Start server

uvicorn main:app --reload

### 7. Open

http://127.0.0.1:8000

API documentation:

http://127.0.0.1:8000/docs
