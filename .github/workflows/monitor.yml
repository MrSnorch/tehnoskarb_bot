name: Monitor Tehnoskarb

on:
  schedule:
    - cron: "*/30 * * * *"   # кожні 30 хвилин
  workflow_dispatch:          # ручний запуск з GitHub для тесту

jobs:
  check:
    runs-on: ubuntu-latest
    permissions:
      contents: write         # дозвіл на коміт seen_products.json

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install requests beautifulsoup4 python-telegram-bot apscheduler

      - name: Run checker
        env:
          BOT_TOKEN: ${{ secrets.BOT_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
        run: python tehnoskarb_check.py

      - name: Save state (seen_products.json)
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add seen_products.json
          git diff --cached --quiet || git commit -m "chore: update seen products [skip ci]"
          git push
