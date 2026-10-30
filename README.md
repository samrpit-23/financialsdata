# Stock Screener Lite (Streamlit + Screener.in data)

**How it works**
1. `scraper.py` pulls company pages from screener.in -> `data/details/*.json` + `data/snapshot.csv`
2. GitHub Actions runs it every weekday evening and commits the data to this repo (free storage)
3. Streamlit Cloud redeploys on each commit; `app.py` only reads local files, so it's instant

**Setup**
1. Create a GitHub repo and push these files.
2. Repo -> Settings -> Actions -> General -> Workflow permissions -> *Read and write*.
3. Actions tab -> "Scrape Screener data" -> Run workflow (first run). If GitHub's servers get blocked (HTTP 403/429),
   run `python scraper.py` on your own PC instead, then `git add data && git commit && git push`.
4. share.streamlit.io -> New app -> pick the repo, main file `app.py` -> Deploy.

**Local run**
    pip install -r requirements.txt
    python scraper.py --limit 10
    streamlit run app.py
