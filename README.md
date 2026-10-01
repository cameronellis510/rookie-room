# The Rookie Room Newsletter

Every Tuesday morning this pulls the ESPN league, has an AI write the recap, and
publishes it to one permanent link.

## One-time setup (about 15 minutes)

1. **Make a GitHub repo** called `rookie-room` (public) and upload everything in this folder,
   including the hidden `.github` folder.
2. **Add your AI key.** Repo > Settings > Secrets and variables > Actions > New repository secret
   - `OPENAI_API_KEY` = your key from platform.openai.com (this is separate from a ChatGPT subscription)
   - Optional: under the Variables tab, set `OPENAI_MODEL` to whatever model you want.
   - To use Claude instead: add `ANTHROPIC_API_KEY` as a secret and set the variable `AI_PROVIDER` to `anthropic`.
3. **Turn on the website.** Settings > Pages > Source: "Deploy from a branch", Branch `main`, folder `/docs`.
4. **Allow the bot to save.** Settings > Actions > General > Workflow permissions > "Read and write permissions".
5. **Test it.** Actions tab > Weekly newsletter > Run workflow.

Your link: `https://<your-github-username>.github.io/rookie-room/`
That link always shows the newest issue. Old issues live at `/weeks/2026-week-03.html` etc.

## Making it funnier

Fill in `lore.md`. Nicknames, rivalries, old trades, how the group chat talks.
That file matters more than which AI writes it.

## Running it by hand

```
pip install -r requirements.txt
python newsletter.py                 # latest finished week
python newsletter.py --week 5        # redo a specific week
python newsletter.py --fixture fixtures/week3.json --copy fixtures/week3_copy.json   # offline preview
```

## Notes
- The ESPN API is unofficial. If ESPN changes it mid-season, the fetch is the one spot to fix.
- The league has to stay public, or you'll need to add your ESPN cookies.
- Cost: hosting and scheduling are free. The AI call is a few cents a week.
