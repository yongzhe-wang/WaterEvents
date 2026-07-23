# tests/datasets — reusable test datasets

Reusable, checked-in **input** datasets for the WaterEvents test harnesses. These are ground-truth / fixtures, NOT run
output — run output (crawl txt, dumps, extraction results) is regenerable and gitignored, never lives here.

| dataset | what it is | shape | used by |
|---------|-----------|-------|---------|
| **`ir_events_media_oracle/`** | 10 real IR events (KO/INTC/XYZ/GILD/CBRE/PM/JBL/GEV/W/VZ) with known metadata + the **real media_urls as an oracle** — the ground truth for evaluating media_agent enrichment (does it recover the true PDF/mp3/webcast/transcript links). | `evNN_<TICKER>.json` = `{id, event_url, known_event:{title,date,type,media_urls[]}}` | `agent/media_agent/smoke.py` (env `MEDIA_DATASET_DIR`) |
| **`ir_pdfs/`** | 20 real IR PDFs (earnings releases / filings) + their source `urls.txt` + a Docling extraction harness (`run.py`). Fixture for the pdf-extraction tool. | `urls.txt` (source urls), `pdfs/NN.pdf` (downloaded), `run.py` (fetch+extract+report). Output `results/` + `results.json` are gitignored/regenerable. | `tests/datasets/ir_pdfs/run.py` |
| **`audio_sample/`** | one IR earnings-call `sample.mp3` + its source `urls.txt`. Fixture for the audio-transcription tool. | `sample.mp3`, `urls.txt` | audio-tool tests |

## Convention

- **Datasets** (here): input fixtures + oracles. Checked in. Renaming/moving a dataset means updating its harness's
  default path (e.g. `MEDIA_DATASET_DIR` in media_agent/smoke.py).
- **Output** (NOT here): `tests/output/`, `tests/media_output/`, `ir_pdfs/results/` — regenerable run artifacts,
  gitignored. Harnesses recreate them (`run5.py` → `tests/output/`, `smoke.py` → `tests/media_output/`).
