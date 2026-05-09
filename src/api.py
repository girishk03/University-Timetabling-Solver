from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import tempfile, json, subprocess, sys
from pathlib import Path

app = FastAPI(
    title="University Timetabling Solver API",
    description="Hybrid CP-SAT + LNS timetabling optimizer",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"status": "ok", "message": "University Timetabling Solver API is live"}

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.post("/solve")
async def solve(file: UploadFile = File(...)):
    if not file.filename.endswith(".xml"):
        raise HTTPException(status_code=400, detail="Only ITC-2019 XML files accepted")
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp_in:
        tmp_in.write(await file.read())
        tmp_in_path = tmp_in.name
    tmp_out_path = tmp_in_path.replace(".xml", "_solution.json")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.run_solver", tmp_in_path, tmp_out_path],
            capture_output=True, text=True, timeout=120
        )
        if not Path(tmp_out_path).exists():
            raise HTTPException(status_code=500, detail=f"Solver failed: {result.stderr[:500]}")
        with open(tmp_out_path) as f:
            return json.load(f)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Solver timed out after 120 seconds")
    finally:
        Path(tmp_in_path).unlink(missing_ok=True)
        Path(tmp_out_path).unlink(missing_ok=True)
