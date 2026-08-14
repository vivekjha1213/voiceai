VoiceAI backend

Run locally:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Seed the DB:

```bash
python -c "from fixtures import seed; seed()"
```
