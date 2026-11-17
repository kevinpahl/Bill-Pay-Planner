# Bill Pay Planner (Streamlit)

Shows which paycheck pays which bill, what's left over from each check, and a running total, projected forward from your Finances.xlsx. The app is locked behind a password.

## Option A: run it on your own computer

1. Install Python 3.10 or newer, then in this folder run:

       pip install -r requirements.txt

2. Set your password: copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and change the `password` line. Delete the `[github]` lines.
3. Start it:

       streamlit run app.py

   It opens at http://localhost:8501. Your data saves to `planner_data.json` in this folder.

## Option B: put it on Streamlit Community Cloud (open it from your phone or anywhere)

1. **Create a private GitHub repo** (github.com, New repository, choose *Private*), for example `bill-planner`. Upload everything in this folder except `.streamlit/secrets.toml` (the `.gitignore` already excludes it if you use git).
2. **Make a GitHub token so the app can save your data.** On GitHub: Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate new token. Under *Repository access* choose *Only select repositories* and pick `bill-planner`. Under *Permissions*, set *Contents* to *Read and write*. Copy the token.
3. **Deploy.** Go to share.streamlit.io, sign in with GitHub, click *Create app*, pick your repo, branch `main`, and main file `app.py`.
4. **Add your secrets.** Before deploying (under *Advanced settings*), or later in the app's *Settings, Secrets*, paste:

       password = "pick-a-long-password"

       [github]
       token = "github_pat_..."
       repo = "your-github-name/bill-planner"

5. **Keep the app private.** An app deployed from a private repo starts out private. In the app's *Share* menu, check it is not public. Only you (and anyone you invite by email) can open it, and the password is a second lock.

Why the GitHub token: Streamlit Cloud erases any files the app writes whenever the app restarts, so on the cloud your bills, paid ticks and moves are saved to a `planner_data.json` file on a separate `planner-data` branch of your repo instead. Saving to a separate branch keeps each save from restarting the app.

## Files

- `app.py`: the Streamlit app (password screen, pages, tables, chart).
- `engine.py`: the rules that pick a paycheck for each bill.
- `data.py`: starting data from your spreadsheet, saving (local file or GitHub), and importing an updated Finances.xlsx.
- `.streamlit/secrets.toml.example`: template for your password and GitHub settings.

## Updating from Excel

Use **Update from Excel** in the sidebar. It expects the same layout as Finances.xlsx: due day in column A, bill name in B, amount in C (starting row 2), first payday in E8, and the two paychecks in F11/G11 and F12/G12 (gross/personal).
