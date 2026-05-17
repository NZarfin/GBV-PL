# Running Locally

```bash
# 1. Pull latest
git pull origin claude/plan-v3-deployment-MuAsb

# 2. Go to app folder
cd gaia-picking-v3

# 3. Activate venv (create once with: python3 -m venv venv)
source venv/bin/activate

# 4. Install dependencies (first time or after changes)
pip install -r requirements.txt

# 5. Set env vars
export SECRET_KEY=hello123
export DB_PATH=picking.db

# 6. Run
python app.py
```

Open **http://localhost:8080**

Login: `nadav@gaiaherbs.nl` / `admin123`

Load test data: http://localhost:8080/test/load
