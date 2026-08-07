name: Crypto Bot Backtest
on:
  workflow_dispatch:        # Actions 탭에서 수동으로 "Run workflow" 눌러서 실행
permissions:
  contents: read
jobs:
  run-backtest:
    runs-on: ubuntu-latest
    timeout-minutes: 120   # 6개월치 x 40종목 x 3타임프레임 수집이라 넉넉히 잡음
    steps:
      - name: Checkout code
        uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - name: Install dependencies
        run: |
          pip install ccxt requests pandas openpyxl
      - name: Run backtest
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          python backtest.py
      - name: Upload result as artifact
        uses: actions/upload-artifact@v4
        with:
          name: backtest-result
          path: backtest_result.xlsx
          retention-days: 30
