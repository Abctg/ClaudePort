# Portfolio analytics dashboard

A private institutional-style analytics site for a 20-holding GBP portfolio (snapshot 16 November 2026). GitHub builds the market data and publishes the site automatically; there is no data to find, download or upload.

## One-time setup (about 5 minutes)

1. **Create a repository** on GitHub (public, so GitHub Pages is free; a private repo needs a paid plan for Pages).
2. **Add the files**, keeping this structure:

   ```
   index.html
   build_data.py
   requirements.txt
   README.md
   .github/workflows/update-data.yml
   ```

   With the web uploader, the `.github` folder is often hidden by Finder/Explorer. If it doesn't upload, use **Add file → Create new file**, type `.github/workflows/update-data.yml` as the name, and paste the file's contents.
3. **Turn on Pages:** Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.
4. **Run the first build:** Actions tab → *Build market data and publish dashboard* → **Run workflow**. It takes 5–10 minutes.
5. Open **https://YOUR-USERNAME.github.io/REPOSITORY-NAME/**.

## What happens automatically

- Every weekday at 06:30 UTC (and on every push) GitHub downloads ten years of daily prices, dividends, exchange rates, benchmarks, yields and factor series from Yahoo Finance and FRED, converts everything to GBP total returns, and republishes the site.
- The dashboard loads that data on its own when the page opens. The pill in the top bar shows the latest data date and when it was last updated.
- If a download fails (for example Yahoo throttles GitHub's servers that day), the site keeps showing the last good data, and the run shows a warning in the Actions tab. If Yahoo fails for an individual ticker, the pipeline falls back to Stooq for that ticker and the Sources & Methodology page flags it as price-only.

## What's in the data

| Item | Source |
|---|---|
| All 20 holdings + 14 candidate instruments | Yahoo Finance dividend-adjusted closes, converted to GBP |
| MSCI World (GBP) | iShares Core MSCI World (SWDA.L) |
| Global bonds hedged to GBP; UK gilts | AGBP.L; IGLT.L |
| FX | GBP/USD, GBP/EUR, GBP/DKK, GBP/CAD (Yahoo) |
| Yields, spreads, GBP risk-free | FRED: US 10y nominal and real, UK and German 10y, US/Euro high-yield spreads, UK 3-month rate |
| Factor proxies | Gold, silver, Brent, VIX, dollar index, growth–value, small–large, EM, semiconductors, defence, uranium proxy |

Notes: Terna uses the Milan shares (TRN.MI) and Novo Nordisk the Copenhagen shares; their GBP returns are equivalent to your lines. EWSP launched in August 2023, so the US equal-weight fund RSP (converted to GBP) is spliced before that and flagged. Theon listed in February 2024; use *Short-history holdings → Exclude* on any analysis page for 5- and 10-year statistics.

## Using it

Your Portfolio Lab weights, saved scenarios and assumptions are stored in your browser for this site. Use **Data → Export weights & assumptions** to keep a copy. **Data → transaction history** is optional and shows your account's actual time- and money-weighted returns.

## Changing holdings later

Edit the holdings table in `index.html` (search for `const HOLDINGS`) and the ticker list at the top of `build_data.py`, then push. The next run picks up the change.

Nothing here is investment advice.
