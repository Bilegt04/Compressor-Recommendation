# Image Compression Recommender

A small web app that takes an image, tries five compression settings, and recommends the best balance between file size and visual quality.

Built as a research prototype. Deployed from GitHub via Docker.

---

## What it does

1. Upload an image (JPEG, PNG, or WebP)
2. The server generates **five variants**: JPEG q90, JPEG q70, WebP q80, WebP q60, AVIF q50
3. Measures **file size**, **PSNR** (technical quality), and **SSIM** (visual similarity) for each
4. Shows you the recommendation with a plain-language explanation:
   > **Recommended: AVIF, quality 50**
   > Smallest file among visually acceptable results.
   > — Saves 32.8% compared with the original.
   > — Keeps SSIM at 0.9767.
   > — Cuts file size by 50.5% vs. JPEG q90.

---

## Local setup

### Prerequisites

- Python 3.10 or newer
- Two system encoders on PATH: `cwebp` and `avifenc`

**macOS:** `brew install webp libavif`
**Ubuntu/Debian:** `sudo apt install webp libavif-bin`
**Windows:** `scoop install libwebp libavif`

Verify with `cwebp -version` and `avifenc --version`.

### Run it

```bash
git clone https://github.com/YOUR_USERNAME/image-compression-recommender.git
cd image-compression-recommender
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env               # optional — defaults are fine
uvicorn backend.main:app --reload --port 8000
```

Open **http://127.0.0.1:8000/ui/**

### Run with Docker (no system encoders needed on host)

```bash
docker build -t compression-recommender .
docker run -p 8000:8000 compression-recommender
```

---

## Deploying to GitHub + Render (recommended)

1. **Push the code**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
   git push -u origin main
   ```

2. **Create a Render account** at https://render.com and connect GitHub.

3. **Create a Blueprint** — on the Render dashboard: **New +** → **Blueprint** → select your repo. Render reads `render.yaml` and spins up the service automatically.

4. **Wait 3–5 minutes** for the first build. The Dockerfile installs `cwebp` and `avifenc` from apt so the service has everything it needs.

5. Visit `https://YOUR_SERVICE.onrender.com/ui/`.

### Free tier caveats

- Service sleeps after ~15 min idle. First request after sleep takes 30–60s.
- Filesystem is ephemeral — uploaded images are wiped on each restart. Fine for test users, not for a persistent corpus.

### Alternative: Fly.io

```bash
fly launch --no-deploy --copy-config   # reads fly.toml
fly deploy
```

Edit `app = "..."` in `fly.toml` first to a globally-unique name.

### Alternative: Railway

Railway auto-detects `Dockerfile`. Connect the repo and hit Deploy — no config file needed.

---

## Configuration

All config via environment variables. See `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `ENABLE_OAM_FEATURES` | `false` | When `true`, exposes thesis/OAM routes (`/exports/*`, `/images/{id}/raw`, `/images/{id}/topsis`) and shows a dev panel in the UI. |
| `MAX_UPLOAD_BYTES` | `26214400` (25 MB) | Upload size limit. |
| `CORS_ORIGINS` | `*` | Comma-separated list of allowed origins. In production, set to your deployed frontend URL. |

---

## Project structure

```
.
├── backend/
│   ├── config.py               # env-var-driven runtime config
│   ├── main.py                 # FastAPI app, route registration
│   └── services/
│       ├── compression.py      # cwebp / avifenc / Pillow wrappers
│       ├── metrics.py          # PSNR, SSIM, size calculations
│       ├── pareto.py           # Pareto front computation
│       ├── recommendation.py   # lexicographic decision rule
│       ├── topsis.py           # alternative decision rule (thesis)
│       ├── presentation.py     # friendly-language translator
│       ├── export_service.py   # OAM CSV exports (thesis)
│       ├── storage.py          # filesystem I/O for data/
│       └── ids.py              # deterministic ID helpers
├── frontend/
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── data/                       # runtime artifacts (gitignored contents)
│   ├── images/
│   ├── variants/
│   ├── results/
│   └── exports/
├── tests/
│   └── test_pipeline.py
├── Dockerfile
├── render.yaml                 # Render.com one-click blueprint
├── fly.toml                    # Fly.io alternative
├── requirements.txt
├── .env.example
├── .gitignore
├── .dockerignore
└── README.md
```

---

## Re-enabling thesis / OAM features

The OAM and TOPSIS code is intact — it's only hidden from the user-facing UI. To turn it back on:

**Locally:**
```bash
ENABLE_OAM_FEATURES=true uvicorn backend.main:app --reload --port 8000
```

**On Render:** change the env var in the service's **Environment** tab → restart.

**On Fly.io:**
```bash
fly secrets set ENABLE_OAM_FEATURES=true
```

When enabled, you get:

- `GET /images` — list all processed images
- `GET /images/{id}/raw` — full internal dataset with rule tags and TOPSIS
- `GET /images/{id}/analysis` — Pareto + recommendation + reasoning
- `GET /images/{id}/topsis` — TOPSIS ranking + comparison with primary rule
- `POST /exports/all?oam_variant={minimal|extended}` — write all four CSVs
- `GET /exports/{raw_results,attribute_dictionary,oam,analysis}.csv` — download
- `GET /coco/preview?oam_variant={minimal|extended}&step_count=N` — JSON with ranked matrix + paste-ready text blocks
- `GET /coco/download?oam_variant=...&step_count=N` — `coco_input.txt` for the external solver

…and a dev panel + COCO Y0 panel in the UI with copy buttons and direct export links.

---

## COCO Y0 export workflow

The thesis methodology uses the external **MIAU COCO Y0 solver** at
`https://miau.my-x.hu/myx-free/coco/beker_y0.php`. This app prepares
solver-ready ranked input but **does not submit anything automatically** —
the external site is treated as a manual external tool.

When `ENABLE_OAM_FEATURES=true`, the UI gains a "COCO Y0 Export" panel with:

- **OAM variant** selector (minimal: 3 attributes / extended: 4)
- **Step count** field (`0` = full dense ranking; `N` = quantile-bin into N steps)
- **Build ranked input** button
- **Ranked matrix** — tab-separated, header row + one row per object (`object_id` + ranks)
- **Object list** and **Attribute list** — separate paste-ready blocks
- **Copy** buttons for each (uses the Clipboard API; falls back to text selection on insecure contexts)
- **Download COCO input as .txt** — saves a self-describing file to `data/exports/coco_input.txt`
- **Open external COCO Y0 solver** link — opens the MIAU page in a new tab

### Ranking logic

For each attribute, raw values are replaced by ranks (1 = best), respecting attribute direction:

| Attribute | Direction | Rank 1 = |
|---|---|---|
| `compressed_size_kb` | minimize | smallest file |
| `psnr` | maximize | highest PSNR |
| `ssim` | maximize | highest SSIM |
| `size_reduction_pct` | maximize | largest reduction |

Ties share a rank (dense ranking). With `step_count > 0`, ranks are collapsed into `step_count` quantile bins.

### Output format

Default is **tab-separated**, matching the de-facto MY-X / MIAU OAM input format. If your COCO Y0 form expects a different separator, change `COCO_SEPARATOR` in `backend/services/coco_export.py` — that's the single point of change.

Example (3 objects, minimal OAM):

```
object_id          compressed_size_kb  psnr  ssim
img001_avif_q50    1                   3     2
img001_jpeg_q90    3                   1     1
img001_webp_q80    2                   2     3
```

---

## API reference

Always-on endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Service info |
| GET | `/config` | Feature flags + limits (used by the UI) |
| GET | `/health` | Health check + encoder availability |
| POST | `/upload` | Upload image → friendly recommendation JSON |
| GET | `/images/{id}` | User view of a previously processed image |
| GET | `/images/{id}/preview` | Original image bytes |
| GET | `/images/{id}/variant/{key}` | Download a variant, e.g. `avif_q50` |
| GET | `/docs` | Interactive API docs (Swagger) |

---

## Testing

```bash
python3 tests/test_pipeline.py
```

Runs end-to-end with stubbed encoders, so it works even without cwebp/avifenc installed. Validates the full pipeline, Pareto invariants, recommendation rules, and — when `ENABLE_OAM_FEATURES=true` — the OAM exports.

---

## Known limitations

- **Free-tier ephemeral storage** — uploads don't persist across deploys or sleeps. Attach a disk or use object storage if you need persistence.
- **No authentication** — deliberately. This is a prototype for invited testers, not a public service. Don't paste the URL on social media without adding rate limiting and auth.
- **Single-worker** — the Docker CMD runs one Uvicorn worker to avoid filesystem races on `data/`. Fine for a handful of concurrent users.
